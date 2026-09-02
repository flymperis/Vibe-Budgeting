from flask import Blueprint, flash, g, request, send_file
from datetime import datetime, timedelta, timezone
from io import BytesIO
from openpyxl import Workbook, load_workbook

from db import get_connection
from excel_io import _build_export_workbook, _build_migration_template_workbook, _run_import_workbook
from helpers import redirect_home

bp = Blueprint("data_io", __name__)


@bp.route("/export/excel")
def export_excel():
    conn = get_connection()
    try:
        wb = _build_export_workbook(conn, g.user_id)
    finally:
        conn.close()

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"budget-export-{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        max_age=0,
    )

@bp.route("/export/migration-template")
def export_migration_template():
    wb = _build_migration_template_workbook()
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="budget-migration-template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        max_age=0,
    )

@bp.route("/import/excel", methods=["POST"])
def import_excel():
    upload = request.files.get("file")
    if not upload or upload.filename.strip() == "":
        flash("Choose an Excel file (.xlsx).", "error")
        return redirect_home(panel="settings", settings_section="migration")

    replace_movements = request.form.get("replace_movements") == "1"
    sync_opening_balances = request.form.get("sync_opening_balances") == "1"

    raw = upload.read()
    if not raw:
        flash("The uploaded file is empty.", "error")
        return redirect_home(panel="settings", settings_section="migration")

    try:
        workbook = load_workbook(BytesIO(raw), data_only=True)
    except Exception as exc:
        flash(f"Could not read the Excel file: {exc}", "error")
        return redirect_home(panel="settings", settings_section="migration")

    errors = _run_import_workbook(workbook, replace_movements, sync_opening_balances, g.user_id)
    if errors:
        preview = "; ".join(errors[:8])
        extra = f" (+{len(errors) - 8} more)" if len(errors) > 8 else ""
        flash(f"Import failed. {preview}{extra}", "error")
        return redirect_home(panel="settings", settings_section="migration")

    flash(
        "Import completed. Accounts/categories from the file were merged (new names added)."
        + (
            " Existing expense and income rows were replaced by the file."
            if replace_movements
            else " Movement rows from the file were added (existing rows kept)."
        ),
        "success",
    )
    return redirect_home(panel="settings", settings_section="migration")
