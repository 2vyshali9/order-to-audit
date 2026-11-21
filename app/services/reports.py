"""Auditor-facing reports. Each report returns a Report: typed columns + raw rows.

Cell kinds: text | date | money (paise; int or Decimal) | qty (Decimal, display unit) | int | pct
Money stays in integer paise until presentation/export.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..money import D, fmt_dec, from_base, inr_plain, state_label
from . import production as prod
from . import sales


@dataclass
class Report:
    key: str
    title: str
    columns: list                 # [(label, kind)]
    rows: list
    sum_cols: tuple = ()          # column indexes to total
    note: str = ""
    totals: list = field(default=None)

    def __post_init__(self):
        if self.sum_cols:
            self.totals = [None] * len(self.columns)
            self.totals[0] = "Total"
            for i in self.sum_cols:
                self.totals[i] = sum((D(r[i]) for r in self.rows if r[i] is not None), Decimal(0))


def fmt_cell(kind, v):
    if v is None or v == "":
        return ""
    if kind == "money":
        return inr_plain(v)
    if kind == "qty":
        return fmt_dec(D(v))
    if kind == "pct":
        return f"{D(v):+.2f}%"
    return str(v)


def month_range(d=None):
    d = d or date.today()
    first = d.replace(day=1)
    nxt = (first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1))
    return first.isoformat(), (nxt.fromordinal(nxt.toordinal() - 1)).isoformat()


# ---------------------------------------------------------------- sales
def sales_register(db, d1, d2, **_):
    rows = []
    for v in db.execute("""SELECT v.*, c.name cname, c.gstin FROM invoices v JOIN customers c ON c.id=v.customer_id
                           WHERE inv_date BETWEEN ? AND ? ORDER BY inv_date, v.id""", (d1, d2)):
        status = "Cancelled" if v["status"] == "cancelled" else ("Dispatched" if v["dispatched_at"] else "Not dispatched")
        rows.append(["Invoice", v["invoice_no"], v["inv_date"], v["cname"], v["gstin"] or "", state_label(v["place_of_supply"]),
                     v["taxable_paise"], v["cgst_paise"], v["sgst_paise"], v["igst_paise"], v["total_paise"], status])
    for n in db.execute("""SELECT n.*, v.invoice_no, c.name cname, c.gstin, v.place_of_supply FROM credit_notes n
                           JOIN invoices v ON v.id=n.invoice_id JOIN customers c ON c.id=v.customer_id
                           WHERE cn_date BETWEEN ? AND ? ORDER BY cn_date, n.id""", (d1, d2)):
        rows.append(["Credit note", n["cn_no"], n["cn_date"], n["cname"], n["gstin"] or "", state_label(n["place_of_supply"]),
                     -n["taxable_paise"], -n["cgst_paise"], -n["sgst_paise"], -n["igst_paise"], -n["total_paise"],
                     f"Against {n['invoice_no']}"])
    cols = [("Type", "text"), ("Number", "text"), ("Date", "date"), ("Customer", "text"), ("GSTIN", "text"),
            ("Place of supply", "text"), ("Taxable", "money"), ("CGST", "money"), ("SGST", "money"), ("IGST", "money"),
            ("Total", "money"), ("Status", "text")]
    return Report("sales_register", "Sales register", cols, rows, (6, 7, 8, 9, 10),
                  "Cancelled invoices appear with their credit note, so the total is net sales.")


def customer_sales(db, d1, d2, **_):
    rows = []
    for r in db.execute("""SELECT c.name, c.type, COUNT(v.id) n, SUM(v.taxable_paise) taxable, SUM(v.total_paise) total,
                             COALESCE((SELECT SUM(p.amount_paise) FROM payments p JOIN invoices x ON x.id=p.invoice_id
                                       WHERE x.customer_id=c.id AND x.inv_date BETWEEN :a AND :b AND x.status='posted'),0) paid
                           FROM customers c JOIN invoices v ON v.customer_id=c.id
                           WHERE v.status='posted' AND v.inv_date BETWEEN :a AND :b GROUP BY c.id ORDER BY total DESC""",
                          {"a": d1, "b": d2}):
        rows.append([r["name"], r["type"], r["n"], r["taxable"], r["total"], r["paid"], r["total"] - r["paid"]])
    cols = [("Customer", "text"), ("Type", "text"), ("Invoices", "int"), ("Taxable", "money"), ("Invoice value", "money"),
            ("Received", "money"), ("Outstanding", "money")]
    return Report("customer_sales", "Customer-wise sales", cols, rows, (2, 3, 4, 5, 6))


def product_sales(db, d1, d2, **_):
    rows = []
    for r in db.execute("""SELECT l.description, SUM(l.qty) qty, SUM(l.taxable_paise) taxable
                           FROM invoice_lines l JOIN invoices v ON v.id=l.invoice_id
                           WHERE v.status='posted' AND v.inv_date BETWEEN ? AND ? GROUP BY l.item_id ORDER BY taxable DESC""",
                          (d1, d2)):
        rows.append([r["description"], r["qty"], r["taxable"], D(r["taxable"]) / D(r["qty"])])
    cols = [("Product", "text"), ("Packets sold", "int"), ("Taxable value", "money"), ("Avg rate / packet", "money")]
    return Report("product_sales", "Product-wise sales", cols, rows, (1, 2))


# ---------------------------------------------------------------- GST
def _outward_lines(db, d1, d2):
    out = []
    for r in db.execute("""SELECT v.invoice_no doc_no, v.inv_date doc_date, v.place_of_supply pos, v.total_paise doc_total,
                                  c.name cname, c.gstin gstin, l.*
                           FROM invoice_lines l JOIN invoices v ON v.id=l.invoice_id JOIN customers c ON c.id=v.customer_id
                           WHERE v.inv_date BETWEEN ? AND ?""", (d1, d2)):
        out.append(("INV", 1, dict(r)))
    for r in db.execute("""SELECT n.cn_no doc_no, n.cn_date doc_date, v.place_of_supply pos, n.total_paise doc_total,
                                  c.name cname, c.gstin gstin, l.*
                           FROM credit_notes n JOIN invoices v ON v.id=n.invoice_id
                           JOIN invoice_lines l ON l.invoice_id=v.id JOIN customers c ON c.id=v.customer_id
                           WHERE n.cn_date BETWEEN ? AND ?""", (d1, d2)):
        out.append(("CN", -1, dict(r)))
    return out


def gst_summary(db, d1, d2, **_):
    agg = defaultdict(lambda: [0, 0, 0, 0])
    for kind, sign, r in _outward_lines(db, d1, d2):
        a = agg[(r["gst_rate_bp"], kind)]
        for i, k in enumerate(("taxable_paise", "cgst_paise", "sgst_paise", "igst_paise")):
            a[i] += sign * r[k]
    rows = []
    for (bp, kind), a in sorted(agg.items()):
        sec = "3.1(a) Outward taxable supplies" if kind == "INV" else "Less: credit notes issued"
        rows.append([sec, D(bp) / 100, a[0], a[3], a[1], a[2], a[1] + a[2] + a[3]])
    cols = [("Section (GSTR-3B)", "text"), ("Rate %", "qty"), ("Taxable value", "money"), ("IGST", "money"),
            ("CGST", "money"), ("SGST", "money"), ("Total tax", "money")]
    return Report("gst_summary", "GST summary of outward supplies (GSTR-3B 3.1)", cols, rows, (2, 3, 4, 5, 6),
                  "Credit notes are shown negative. Net totals map to GSTR-3B table 3.1(a); invoice-level detail is in the GSTR-1 report.")


def gstr1(db, d1, d2, **_):
    agg = {}
    for kind, sign, r in _outward_lines(db, d1, d2):
        key = (kind, r["doc_no"], r["gst_rate_bp"])
        a = agg.setdefault(key, {"r": r, "sign": sign, "v": [0, 0, 0, 0]})
        for i, k in enumerate(("taxable_paise", "cgst_paise", "sgst_paise", "igst_paise")):
            a["v"][i] += sign * r[k]
    rows = []
    for (kind, no, bp), a in sorted(agg.items(), key=lambda kv: (kv[1]["r"]["doc_date"], kv[0][1], kv[0][2])):
        r = a["r"]
        section = ("B2B" if r["gstin"] else "B2C") if kind == "INV" else "CDNR"
        rows.append([section, r["gstin"] or "", r["cname"], no, r["doc_date"], a["sign"] * r["doc_total"],
                     state_label(r["pos"]), D(bp) / 100, a["v"][0], a["v"][3], a["v"][1], a["v"][2]])
    cols = [("Section", "text"), ("Recipient GSTIN", "text"), ("Recipient", "text"), ("Document no.", "text"),
            ("Date", "date"), ("Document value", "money"), ("Place of supply", "text"), ("Rate %", "qty"),
            ("Taxable", "money"), ("IGST", "money"), ("CGST", "money"), ("SGST", "money")]
    rep = Report("gstr1", "GSTR-1 data: B2B / B2C / credit notes (rate-wise)", cols, rows, (8, 9, 10, 11),
                 "One row per document per tax rate, laid out like GSTR-1 tables 4 (B2B), 7 (B2C) and 9B (CDNR).")
    return rep


def hsn_summary(db, d1, d2, **_):
    agg = defaultdict(lambda: [0, 0, 0, 0, 0])
    for kind, sign, r in _outward_lines(db, d1, d2):
        a = agg[(r["hsn"] or "", r["description"], r["gst_rate_bp"])]
        a[0] += sign * r["qty"]
        for i, k in enumerate(("taxable_paise", "igst_paise", "cgst_paise", "sgst_paise"), 1):
            a[i] += sign * r[k]
    rows = [[h, desc, "PKT", a[0], D(bp) / 100, a[1], a[2], a[3], a[4]] for (h, desc, bp), a in sorted(agg.items())]
    cols = [("HSN", "text"), ("Description", "text"), ("UQC", "text"), ("Qty", "int"), ("Rate %", "qty"),
            ("Taxable", "money"), ("IGST", "money"), ("CGST", "money"), ("SGST", "money")]
    return Report("hsn_summary", "HSN-wise summary of outward supplies (GSTR-1 table 12)", cols, rows, (3, 5, 6, 7, 8))


# ---------------------------------------------------------------- purchases
def purchase_register(db, d1, d2, **_):
    rows = []
    for r in db.execute("""SELECT p.purchase_no, p.purchase_date, p.supplier_inv_no, s.name sname, s.gstin, l.*, i.name iname,
                                  i.disp_unit, i.factor
                           FROM purchase_lines l JOIN purchases p ON p.id=l.purchase_id JOIN items i ON i.id=l.item_id
                           LEFT JOIN suppliers s ON s.id=p.supplier_id
                           WHERE p.purchase_date BETWEEN ? AND ? ORDER BY p.purchase_date, p.id, l.id""", (d1, d2)):
        rows.append([r["purchase_no"], r["purchase_date"], r["sname"] or "", r["gstin"] or "", r["supplier_inv_no"] or "",
                     r["iname"], D(r["qty_base"]) / r["factor"], r["disp_unit"], r["rate_paise"], r["amount_paise"],
                     D(r["gst_rate_bp"]) / 100, r["gst_paise"], r["amount_paise"] + r["gst_paise"], r["batch_no"] or ""])
    cols = [("Purchase no.", "text"), ("Date", "date"), ("Supplier", "text"), ("Supplier GSTIN", "text"),
            ("Supplier invoice", "text"), ("Item", "text"), ("Qty", "qty"), ("Unit", "text"), ("Rate / unit", "money"),
            ("Taxable", "money"), ("GST %", "qty"), ("GST", "money"), ("Total", "money"), ("Batch", "text")]
    return Report("purchase_register", "Purchase register", cols, rows, (9, 11, 12))


# ---------------------------------------------------------------- stock
def stock_summary(db, d1, d2, kind=None, **_):
    rows = []
    sql = """SELECT i.*,
      COALESCE(SUM(CASE WHEN (l.mtype='OPENING' AND l.mov_date<=:b) OR l.mov_date<:a THEN l.qty_base END),0) oq,
      COALESCE(SUM(CASE WHEN l.mtype NOT IN ('OPENING','ADJUSTMENT') AND l.mov_date BETWEEN :a AND :b AND l.qty_base>0 THEN l.qty_base END),0) rq,
      COALESCE(SUM(CASE WHEN l.mtype NOT IN ('OPENING','ADJUSTMENT') AND l.mov_date BETWEEN :a AND :b AND l.qty_base<0 THEN -l.qty_base END),0) iq,
      COALESCE(SUM(CASE WHEN l.mtype='ADJUSTMENT' AND l.mov_date BETWEEN :a AND :b THEN l.qty_base END),0) aq,
      COALESCE(SUM(CASE WHEN l.mov_date<=:b THEN l.value_paise END),0) cv
      FROM items i LEFT JOIN stock_ledger l ON l.item_id=i.id WHERE i.active=1 {kind}
      GROUP BY i.id ORDER BY CASE i.kind WHEN 'raw' THEN 1 WHEN 'packaging' THEN 2 ELSE 3 END, i.name"""
    args = {"a": d1, "b": d2}
    kind_sql = ""
    if kind:
        kind_sql, args["k"] = "AND i.kind=:k", kind
    for r in db.execute(sql.format(kind=kind_sql), args):
        f = r["factor"]
        closing = r["oq"] + r["rq"] - r["iq"] + r["aq"]
        alert = "LOW" if (r["kind"] != "finished" and r["reorder_level_base"] and closing < r["reorder_level_base"]) else ""
        rows.append([r["name"], r["kind"], r["disp_unit"], D(r["oq"]) / f, D(r["rq"]) / f, D(r["iq"]) / f, D(r["aq"]) / f,
                     D(closing) / f, r["cv"], alert])
    cols = [("Item", "text"), ("Kind", "text"), ("Unit", "text"), ("Opening", "qty"), ("Receipts", "qty"), ("Issues / sales", "qty"),
            ("Adjustments", "qty"), ("Closing", "qty"), ("Closing value", "money"), ("Reorder", "text")]
    return Report("stock_summary", "Stock summary", cols, rows, (8,),
                  "Opening + receipts - issues +/- adjustments = closing. Valued at weighted-average cost. LOW = below reorder level.")


def stock_ledger(db, d1, d2, item_id=None, kind=None, **_):
    rows = []
    items = db.execute("SELECT * FROM items WHERE active=1" + (" AND id=?" if item_id else "") +
                       (" AND kind=?" if kind and not item_id else "") + " ORDER BY kind, name",
                       [x for x in (item_id, None if item_id else kind) if x]).fetchall()
    for it in items:
        f = it["factor"]
        bf = db.execute("SELECT COALESCE(SUM(qty_base),0) q FROM stock_ledger WHERE item_id=? AND mov_date<?",
                        (it["id"], d1)).fetchone()["q"]
        running = bf
        label = f"{it['name']} ({it['disp_unit']})"
        if bf:
            rows.append(["", label, "B/F", "Brought forward", "", "", None, None, D(running) / f, None, ""])
        for m in db.execute("""SELECT l.*, u.username FROM stock_ledger l LEFT JOIN users u ON u.id=l.user_id
                               WHERE item_id=? AND mov_date BETWEEN ? AND ? ORDER BY l.id""", (it["id"], d1, d2)):
            running += m["qty_base"]
            q = D(m["qty_base"]) / f
            rows.append([m["mov_date"], label, m["mtype"], m["ref_no"] or m["ref_type"], m["batch_no"] or "", m["expiry"] or "",
                         q if m["qty_base"] > 0 else None, -q if m["qty_base"] < 0 else None, D(running) / f,
                         m["value_paise"], m["username"] or ""])
    cols = [("Date", "date"), ("Item", "text"), ("Movement", "text"), ("Source document", "text"), ("Batch", "text"),
            ("Expiry", "date"), ("In", "qty"), ("Out", "qty"), ("Balance", "qty"), ("Value", "money"), ("User", "text")]
    return Report("stock_ledger", "Stock ledger", cols, rows, note="Every movement with its source document; balances are running sums of this ledger.")


# ---------------------------------------------------------------- production & costing
def production_report(db, d1, d2, **_):
    rows = []
    for r in db.execute("SELECT id FROM runs WHERE run_date BETWEEN ? AND ? AND status<>'cancelled' ORDER BY id", (d1, d2)):
        s = prod.run_summary(db, r["id"])
        run = s["run"]
        for l in s["lines"]:
            rows.append([run["run_no"], run["run_date"], run["product"], l["name"], l["disp_unit"],
                         D(l["planned_base"]) / l["factor"], D(l["actual_base"]) / l["factor"],
                         D(l["var_base"]) / l["factor"], l["var_pct"], "FLAG" if l["flag"] else ""])
        if run["status"] == "completed":
            rows.append([run["run_no"], run["run_date"], run["product"], "Yield (good packets)", "packet",
                         D(run["expected_output"]), D(run["good_output"]), D(run["good_output"] - run["expected_output"]),
                         s["yield_pct"], "FLAG" if s["yield_flag"] else ""])
            rows.append([run["run_no"], run["run_date"], run["product"], "Wastage: damaged pouches", "piece",
                         None, D(run["damaged_units"]), None, None, ""])
        else:
            rows.append([run["run_no"], run["run_date"], run["product"], "(run still open)", "", None, None, None, None, ""])
    cols = [("Run", "text"), ("Date", "date"), ("Product", "text"), ("Item", "text"), ("Unit", "text"), ("Planned", "qty"),
            ("Actual", "qty"), ("Variance", "qty"), ("Variance %", "pct"), ("Flag", "text")]
    return Report("production_report", "Production: planned vs actual consumption, yield and wastage", cols, rows,
                  note=f"FLAG = variance beyond the tolerance of {prod.tolerance(db)}% either way.")


def costing_report(db, d1, d2, **_):
    rows = []
    for r in db.execute("SELECT id FROM runs WHERE run_date BETWEEN ? AND ? AND status='completed' ORDER BY id", (d1, d2)):
        s = prod.run_summary(db, r["id"])
        run = s["run"]
        rows.append([run["run_no"], run["run_date"], run["product"], run["good_output"], s["materials_paise"],
                     s["overhead_paise"], s["total_paise"], s["cost_per_packet"], run["price_paise"],
                     s["margin_per_packet"], s["margin_pct"] and s["margin_pct"].quantize(Decimal("0.1"))])
    cols = [("Run", "text"), ("Date", "date"), ("Product", "text"), ("Good packets", "int"), ("Materials", "money"),
            ("Overheads", "money"), ("Total cost", "money"), ("Cost / packet", "money"), ("Selling price", "money"),
            ("Margin / packet", "money"), ("Margin %", "qty")]
    return Report("costing_report", "Costing: cost per packet and margin by run", cols, rows, (3, 4, 5, 6),
                  "Materials at weighted-average cost; overheads charged directly to the run; cost per packet = total cost / GOOD packets "
                  "(yield loss and damaged pouches are absorbed by the good output).")


# ---------------------------------------------------------------- money in
def payments_report(db, d1, d2, **_):
    rows = [[p["receipt_no"], p["pay_date"], p["cname"], p["invoice_no"], p["mode"], p["ref"] or "", p["amount_paise"]]
            for p in db.execute("""SELECT p.*, c.name cname, v.invoice_no FROM payments p JOIN customers c ON c.id=p.customer_id
                                   JOIN invoices v ON v.id=p.invoice_id WHERE pay_date BETWEEN ? AND ? ORDER BY pay_date, p.id""",
                                (d1, d2))]
    cols = [("Receipt", "text"), ("Date", "date"), ("Customer", "text"), ("Invoice", "text"), ("Mode", "text"),
            ("Reference", "text"), ("Amount", "money")]
    return Report("payments_report", "Payments and collections", cols, rows, (6,))


def _bucket(days):
    return 0 if days <= 30 else 1 if days <= 60 else 2 if days <= 90 else 3


def outstanding_report(db, d1, d2, **_):
    """Receivables by customer with ageing, as of the 'to' date."""
    asof = date.fromisoformat(d2)
    per = defaultdict(lambda: [0, 0, 0, 0])
    for v in db.execute("""SELECT v.id, v.inv_date, c.name FROM invoices v JOIN customers c ON c.id=v.customer_id
                           WHERE v.status='posted' AND v.inv_date<=?""", (d2,)):
        due = db.execute(
            "SELECT v.total_paise - COALESCE((SELECT SUM(amount_paise) FROM payments WHERE invoice_id=v.id AND pay_date<=?),0) d "
            "FROM invoices v WHERE v.id=?", (d2, v["id"])).fetchone()["d"]
        if due:
            per[v["name"]][_bucket((asof - date.fromisoformat(v["inv_date"])).days)] += due
    rows = [[name, *b, sum(b)] for name, b in sorted(per.items())]
    cols = [("Customer", "text"), ("0-30 days", "money"), ("31-60 days", "money"), ("61-90 days", "money"),
            ("Over 90 days", "money"), ("Total outstanding", "money")]
    return Report("outstanding_report", f"Outstanding receivables with ageing (as of {d2})", cols, rows, (1, 2, 3, 4, 5),
                  "Age = days from invoice date to the 'to' date.")


def outstanding_invoices(db, d1, d2, **_):
    asof = date.fromisoformat(d2)
    rows = []
    for v in db.execute("""SELECT v.*, c.name cname FROM invoices v JOIN customers c ON c.id=v.customer_id
                           WHERE v.status='posted' AND v.inv_date<=? ORDER BY v.inv_date, v.id""", (d2,)):
        paid = db.execute("SELECT COALESCE(SUM(amount_paise),0) p FROM payments WHERE invoice_id=? AND pay_date<=?",
                          (v["id"], d2)).fetchone()["p"]
        if v["total_paise"] - paid:
            days = (asof - date.fromisoformat(v["inv_date"])).days
            rows.append([v["invoice_no"], v["inv_date"], v["cname"], v["total_paise"], paid, v["total_paise"] - paid, days,
                         ["0-30", "31-60", "61-90", "90+"][_bucket(days)]])
    cols = [("Invoice", "text"), ("Date", "date"), ("Customer", "text"), ("Invoice value", "money"), ("Received", "money"),
            ("Outstanding", "money"), ("Days", "int"), ("Bucket", "text")]
    return Report("outstanding_invoices", f"Outstanding invoices (as of {d2})", cols, rows, (3, 4, 5))


def audit_report(db, d1, d2, **_):
    rows = [[a["ts"], a["username"] or "", a["entity"], a["entity_id"], a["action"], a["details"] or ""]
            for a in db.execute("SELECT * FROM audit_log WHERE substr(ts,1,10) BETWEEN ? AND ? ORDER BY id", (d1, d2))]
    cols = [("Time", "text"), ("User", "text"), ("Entity", "text"), ("ID", "int"), ("Action", "text"), ("Details", "text")]
    return Report("audit_report", "Audit trail", cols, rows)


# key -> (group, function)
REGISTRY = {
    "sales_register": ("Sales", sales_register), "customer_sales": ("Sales", customer_sales),
    "product_sales": ("Sales", product_sales),
    "gst_summary": ("GST", gst_summary), "gstr1": ("GST", gstr1), "hsn_summary": ("GST", hsn_summary),
    "purchase_register": ("Purchases", purchase_register),
    "stock_summary": ("Stock", stock_summary), "stock_ledger": ("Stock", stock_ledger),
    "production_report": ("Production", production_report), "costing_report": ("Production", costing_report),
    "payments_report": ("Receivables", payments_report), "outstanding_report": ("Receivables", outstanding_report),
    "outstanding_invoices": ("Receivables", outstanding_invoices),
    "audit_report": ("Audit", audit_report),
}


def build(db, key, d1, d2, **opts):
    return REGISTRY[key][1](db, d1, d2, **opts)
