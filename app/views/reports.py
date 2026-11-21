"""Reports, exports and the owner dashboard."""
from decimal import Decimal

from flask import Blueprint, Response, abort, g, render_template, request

from .. import exporters
from ..money import D
from ..services import production as pr
from ..services import reports as rp
from ..services import stock
from ..services.common import setting, today
from .helpers import get_db, roles_required

bp = Blueprint("reports", __name__)

TITLES = {
    "sales_register": "Sales register", "customer_sales": "Customer-wise sales", "product_sales": "Product-wise sales",
    "gst_summary": "GST summary (GSTR-3B)", "gstr1": "GSTR-1 data (B2B/B2C/CDNR)", "hsn_summary": "HSN summary",
    "purchase_register": "Purchase register", "stock_summary": "Stock summary", "stock_ledger": "Stock ledger",
    "production_report": "Production: planned vs actual", "costing_report": "Cost per packet & margin",
    "payments_report": "Payments & collections", "outstanding_report": "Outstanding with ageing",
    "outstanding_invoices": "Outstanding by invoice", "audit_report": "Audit trail",
}
PRODUCTION_ALLOWED = {"stock_summary", "stock_ledger", "production_report", "costing_report"}


def _allowed(key):
    if g.user["role"] == "production":
        return key in PRODUCTION_ALLOWED
    return g.user["role"] in ("admin", "accountant", "owner")


@bp.route("/reports")
@roles_required("admin", "accountant", "owner", "production")
def index():
    groups = {}
    for key, (grp, _fn) in rp.REGISTRY.items():
        if _allowed(key):
            groups.setdefault(grp, []).append((key, TITLES[key]))
    d1, d2 = rp.month_range()
    return render_template("reports_index.html", groups=groups, d1=d1, d2=d2)


@bp.route("/reports/<key>")
@roles_required("admin", "accountant", "owner", "production")
def report(key):
    if key not in rp.REGISTRY:
        abort(404)
    if not _allowed(key):
        abort(403)
    db = get_db()
    d1, d2 = request.args.get("from"), request.args.get("to")
    if not (d1 and d2):
        d1, d2 = rp.month_range()
    opts = {"kind": request.args.get("kind") or None, "item_id": int(request.args["item"]) if request.args.get("item") else None}
    rep = rp.build(db, key, d1, d2, **opts)
    fmt = request.args.get("fmt", "html")
    fname = f"{key}_{d1}_{d2}"
    if fmt == "csv":
        return Response(exporters.to_csv(rep), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={fname}.csv"})
    if fmt == "xlsx":
        return Response(exporters.to_xlsx(rep), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={fname}.xlsx"})
    if fmt == "pdf":
        return Response(exporters.to_pdf(rep, setting(db, "company_name"), f"{d1} to {d2}"), mimetype="application/pdf",
                        headers={"Content-Disposition": f"inline; filename={fname}.pdf"})
    items = db.execute("SELECT id, name FROM items WHERE active=1 ORDER BY name").fetchall() if key == "stock_ledger" else []
    return render_template("report.html", rep=rep, key=key, d1=d1, d2=d2, kind=opts["kind"] or "", item=opts["item_id"], items=items,
                           qs=request.query_string.decode())


@bp.route("/dashboard")
@roles_required("owner", "admin")
def dashboard():
    db = get_db()
    d1, d2 = rp.month_range()
    t = today()
    status = db.execute("SELECT status, COUNT(*) n FROM orders WHERE substr(created_at,1,10)=? GROUP BY status", (t,)).fetchall()
    month_sales = db.execute("SELECT COALESCE(SUM(taxable_paise),0) s FROM invoices WHERE status='posted' AND inv_date BETWEEN ? AND ?",
                             (d1, d2)).fetchone()["s"]
    out = rp.outstanding_report(db, d1, t if t < d2 else d2)
    low = [r for r in stock.stock_levels(db) if r["kind"] != "finished" and r["reorder_level_base"] and r["qty"] < r["reorder_level_base"]]
    costing = rp.costing_report(db, "2000-01-01", "2999-12-31")
    by_product = {}
    for row in costing.rows:  # last run per product
        by_product[row[2]] = row
    return render_template("dashboard.html", status=status, month_sales=month_sales, levels=stock.stock_levels(db), low=low,
                           alerts=pr.variance_alerts(db), outstanding=out, cost_rows=list(by_product.values()), today=t,
                           open_orders=db.execute("SELECT COUNT(*) n FROM orders WHERE status IN ('submitted','approved')").fetchone()["n"])
