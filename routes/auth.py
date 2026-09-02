from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
import sqlite3

from config import ALLOW_REGISTRATION, _DUMMY_PASSWORD_HASH, _USERNAME_RE
from db import get_connection, seed_user_defaults
from helpers import _safe_next_url

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        nxt = _safe_next_url(request.args.get("next"))
        return redirect(nxt or url_for("dashboard.index"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        finally:
            conn.close()
        # Always run a hash comparison so response timing does not reveal
        # whether the username exists (mitigates user enumeration).
        password_hash = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
        password_ok = check_password_hash(password_hash, password)
        if row and password_ok:
            session["user_id"] = row["id"]
            session["username"] = username
            nxt = _safe_next_url(request.form.get("next")) or _safe_next_url(request.args.get("next"))
            return redirect(nxt or url_for("dashboard.index"))
        flash("Invalid username or password.", "error")
    return render_template("login.html", next=request.args.get("next") or "", allow_registration=ALLOW_REGISTRATION)

@bp.route("/register", methods=["GET", "POST"])
def register():
    if not ALLOW_REGISTRATION:
        flash("Registration is disabled.", "error")
        return redirect(url_for("auth.login"))
    if session.get("user_id"):
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""
        if not _USERNAME_RE.fullmatch(username):
            flash("Username must be 3–32 characters (letters, digits, . _ -).", "error")
            return render_template("register.html")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("register.html")
        if password != password2:
            flash("Passwords do not match.", "error")
            return render_template("register.html")
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
            uid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            seed_user_defaults(conn, uid)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            flash("That username is already taken.", "error")
            return render_template("register.html")
        conn.close()
        session["user_id"] = uid
        session["username"] = username
        flash("Account created.", "success")
        return redirect(url_for("dashboard.index"))
    return render_template("register.html")

@bp.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    session.pop("username", None)
    return redirect(url_for("auth.login"))
