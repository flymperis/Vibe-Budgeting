from flask import Blueprint, g, request

from db import get_connection
from helpers import redirect_home

bp = Blueprint("categories", __name__)


@bp.route("/categories/add", methods=["POST"])
def add_category():
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO categories(user_id, name) VALUES (?, ?)",
            (g.user_id, name),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/categories/<int:category_id>/edit", methods=["POST"])
def edit_category(category_id):
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "UPDATE categories SET name = ? WHERE id = ? AND user_id = ?",
            (name, category_id, g.user_id),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/categories/<int:category_id>/delete", methods=["POST"])
def delete_category(category_id):
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM categories WHERE id = ? AND user_id = ?",
        (category_id, g.user_id),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        print(f"[vibe-budgeting] refused category delete {category_id} (still referenced or missing)")
    return redirect_home()

@bp.route("/income-categories/add", methods=["POST"])
def add_income_category():
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO income_categories(user_id, name) VALUES (?, ?)",
            (g.user_id, name),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/income-categories/<int:category_id>/edit", methods=["POST"])
def edit_income_category(category_id):
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "UPDATE income_categories SET name = ? WHERE id = ? AND user_id = ?",
            (name, category_id, g.user_id),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/income-categories/<int:category_id>/delete", methods=["POST"])
def delete_income_category(category_id):
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM income_categories WHERE id = ? AND user_id = ?",
        (category_id, g.user_id),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        print(f"[vibe-budgeting] refused income category delete {category_id} (still referenced or missing)")
    return redirect_home()
