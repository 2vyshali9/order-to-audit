"""Raw-material and packaging purchases (receipts into stock)."""
from datetime import date, timedelta

from ..db import tx
from ..money import D, rnd, to_base
from . import stock
from .common import BusinessError, audit, get_item, next_no, now_ts, today


def create_purchase(db, supplier_id, lines, user, supplier_inv_no=None, purchase_date=None):
    """lines: [{item_id, qty, unit, rate_paise (per DISPLAY unit), gst_rate_bp?, batch_no?, expiry?}]"""
    purchase_date = purchase_date or today()
    if not lines:
        raise BusinessError("Add at least one line")
    with tx(db):
        n = next_no(db, "purchase")
        pno = f"PUR-{n:04d}"
        cur = db.execute(
            "INSERT INTO purchases(purchase_no,supplier_id,supplier_inv_no,purchase_date,created_by,created_at) VALUES (?,?,?,?,?,?)",
            (pno, supplier_id, supplier_inv_no, purchase_date, user["id"], now_ts()))
        pid = cur.lastrowid
        total = 0
        for ln in lines:
            item = get_item(db, ln["item_id"])
            if item["kind"] == "finished":
                raise BusinessError("Finished goods are produced, not purchased")
            qty = to_base(ln["qty"], ln.get("unit"), item)
            if qty <= 0 or ln["rate_paise"] < 0:
                raise BusinessError("Quantity must be positive and rate non-negative")
            amount = rnd(D(qty) * ln["rate_paise"] / item["factor"])   # taxable value
            gst_bp = int(ln.get("gst_rate_bp") or 0)
            gst = rnd(D(amount) * gst_bp / 10000)
            expiry = ln.get("expiry")
            if not expiry and item["shelf_life_days"]:
                expiry = (date.fromisoformat(purchase_date) + timedelta(days=item["shelf_life_days"])).isoformat()
            batch = ln.get("batch_no") or f"{pno}"
            db.execute(
                """INSERT INTO purchase_lines(purchase_id,item_id,qty_base,rate_paise,amount_paise,gst_rate_bp,gst_paise,
                      batch_no,expiry) VALUES (?,?,?,?,?,?,?,?,?)""",
                (pid, item["id"], qty, ln["rate_paise"], amount, gst_bp, gst, batch, expiry))
            # stock is valued ex-GST (input tax credit is recoverable)
            stock.receive(db, item["id"], qty, amount, mtype="PURCHASE", ref_type="purchase", ref_id=pid, ref_no=pno,
                          user=user, on_date=purchase_date, batch_no=batch, expiry=expiry)
            total += amount + gst
        audit(db, user, "purchase", pid, "create", {"purchase_no": pno, "total_paise": total})
    return pid
