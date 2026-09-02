from flask import Blueprint, current_app, flash, g, request
from urllib.error import HTTPError, URLError
import integrations
import json
import telegram_bot

from db import get_connection
from helpers import _integrations_from_request, redirect_home

bp = Blueprint("settings", __name__)


@bp.route("/settings/integrations/save", methods=["POST"])
def save_integrations():
    settings = _integrations_from_request()
    if settings is None:
        return redirect_home(panel="settings", settings_section="integrations")
    conn = get_connection()
    integrations.save_user_integrations(conn, g.user_id, settings)
    conn.close()
    flash("Integration settings saved.", "success")
    return redirect_home(panel="settings", settings_section="integrations")

@bp.route("/settings/integrations/test", methods=["POST"])
def test_integrations():
    settings = _integrations_from_request()
    if settings is None:
        return redirect_home(panel="settings", settings_section="integrations")
    ok, message = integrations.test_ai_connection(settings)
    flash(message, "success" if ok else "error")
    return redirect_home(panel="settings", settings_section="integrations")

@bp.route("/settings/integrations/models")
def list_integration_models():
    base_url = (request.args.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return current_app.response_class(
            response=json.dumps({"error": "Base URL is required."}),
            status=400,
            mimetype="application/json",
        )
    try:
        integrations._normalize_base_url(base_url)
    except ValueError as exc:
        return current_app.response_class(
            response=json.dumps({"error": str(exc)}),
            status=400,
            mimetype="application/json",
        )
    timeout = integrations.DEFAULT_AI_TIMEOUT
    try:
        models = integrations.fetch_ollama_models(base_url, timeout=timeout)
    except HTTPError as exc:
        return current_app.response_class(
            response=json.dumps({"error": f"Ollama responded with HTTP {exc.code}."}),
            status=502,
            mimetype="application/json",
        )
    except URLError as exc:
        return current_app.response_class(
            response=json.dumps({"error": f"Could not reach Ollama: {exc.reason}"}),
            status=502,
            mimetype="application/json",
        )
    except (json.JSONDecodeError, OSError, TimeoutError) as exc:
        return current_app.response_class(
            response=json.dumps({"error": f"Connection failed: {exc}"}),
            status=502,
            mimetype="application/json",
        )
    return current_app.response_class(
        response=json.dumps({"models": models}),
        status=200,
        mimetype="application/json",
    )

@bp.route("/settings/telegram/server", methods=["POST"])
def save_telegram_server():
    conn = get_connection()
    existing = telegram_bot.get_server_config(conn)
    try:
        settings = telegram_bot.parse_server_config_form(request.form, existing)
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return redirect_home(panel="settings", settings_section="integrations")
    telegram_bot.save_server_config(conn, settings)
    ok, message = telegram_bot.clear_webhook(settings)
    conn.close()
    flash("Telegram settings saved. Polling mode — no public URL needed.", "success")
    flash(message, "success" if ok else "error")
    return redirect_home(panel="settings", settings_section="integrations")

@bp.route("/settings/telegram/test", methods=["POST"])
def test_telegram_server():
    conn = get_connection()
    existing = telegram_bot.get_server_config(conn)
    try:
        settings = telegram_bot.parse_server_config_form(request.form, existing)
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return redirect_home(panel="settings", settings_section="integrations")
    ok, message = telegram_bot.test_telegram_connection(settings)
    conn.close()
    flash(message, "success" if ok else "error")
    return redirect_home(panel="settings", settings_section="integrations")

@bp.route("/settings/telegram/generate-code", methods=["POST"])
def telegram_generate_code():
    conn = get_connection()
    config = telegram_bot.get_server_config(conn)
    if not telegram_bot.is_configured(config):
        conn.close()
        flash("Configure the Telegram bot below first.", "error")
        return redirect_home(panel="settings", settings_section="integrations")
    code = telegram_bot.create_link_code(conn, g.user_id)
    conn.close()
    flash(f"Link code: {code} (15 min). In Telegram send: /link {code}", "success")
    return redirect_home(panel="settings", settings_section="integrations")

@bp.route("/settings/telegram/unlink", methods=["POST"])
def telegram_unlink():
    conn = get_connection()
    telegram_bot.unlink_telegram(conn, g.user_id)
    conn.close()
    flash("Telegram unlinked.", "success")
    return redirect_home(panel="settings", settings_section="integrations")

@bp.route("/settings/telegram/default-account", methods=["POST"])
def telegram_default_account():
    raw = request.form.get("default_account_id", "").strip()
    if raw:
        try:
            account_id = int(raw)
        except (TypeError, ValueError):
            flash("Could not update default account.", "error")
            return redirect_home(panel="settings", settings_section="integrations")
    else:
        account_id = None
    conn = get_connection()
    if not telegram_bot.set_default_account(conn, g.user_id, account_id):
        conn.close()
        flash("Could not update default account.", "error")
        return redirect_home(panel="settings", settings_section="integrations")
    conn.close()
    flash("Telegram default account updated.", "success")
    return redirect_home(panel="settings", settings_section="integrations")
