"""Sales loop: order -> verification -> GST invoice -> dispatch -> payment -> credit note."""
import json
import secrets
from decimal import Decimal
from math import ceil

from ..db import tx
from ..money import D, fy_of, line_tax, to_base
from . import stock
from .common import (BusinessError, ShortageError, audit, get_item, next_no, now_ts, setting, today)


# ---- orders ---------------------------------------------------------------
def price_for(db, customer_id, item_id) -> int:
    row = db.execute("SELECT price_paise FROM customer_prices WHERE customer_id=? AND item_id=?",
                     (customer_id, item_id)).fetchone()
    return row["price_paise"] if row else get_item(db, item_id)["price_paise"]


def create_order(db, customer_id, salesman, lines, client_uuid=None, notes=None):
    """lines: [{item_id, qty, unit}]. Idempotent on client_uuid (offline retries)."""
    if client_uuid:
        existing = db.execute("SELECT id FROM orders WHERE client_uuid=?", (client_uuid,)).fetchone()
        if existing:
            return existing["id"], False
    cust = db.execute("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone()
    if not cust:
        raise BusinessError("Unknown customer")
    merged = {}
    for ln in lines:
        item = get_item(db, ln["item_id"])
        if item["kind"] != "finished":
            raise BusinessError(f"{item['name']} is not a sellable product")
        qty = to_base(ln["qty"], ln.get("unit") or "packet", item)
        if qty < 0:
            raise BusinessError("Quantity cannot be negative")
        if qty:
            merged[item["id"]] = merged.get(item["id"], 0) + qty
    if not merged:
        raise BusinessError("Add at least one product with a quantity")
    with tx(db):
        n = next_no(db, "order")
        order_no = f"ORD-{n:04d}"
        ts = now_ts()
        cur = db.execute(
            """INSERT INTO orders(order_no, client_uuid, customer_id, salesman_id, status, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (order_no, client_uuid, customer_id, salesman["id"], "submitted", notes, ts, ts))
        oid = cur.lastrowid
        for item_id, qty in merged.items():
            db.execute("INSERT INTO order_lines(order_id,item_id,qty,price_paise) VALUES (?,?,?,?)",
                       (oid, item_id, qty, price_for(db, customer_id, item_id)))
        audit(db, salesman, "order", oid, "create", {"order_no": order_no, "lines": merged})
    return oid, True


def order_lines(db, order_id):
    return db.execute(
        """SELECT ol.*, i.name, i.code, i.carton_size FROM order_lines ol JOIN items i ON i.id=ol.item_id
           WHERE ol.order_id=? ORDER BY ol.id""", (order_id,)).fetchall()


def order_value(db, order_id) -> int:
    return db.execute("SELECT COALESCE(SUM(qty*price_paise),0) v FROM order_lines WHERE order_id=?",
                      (order_id,)).fetchone()["v"]


def stock_check(db, order_id):
    """Per line: on-hand FG, reserved by OTHER approved/invoiced orders, available, shortage."""
    out = []
    for ln in order_lines(db, order_id):
        on_hand, _ = stock.balance(db, ln["item_id"])
        reserved = db.execute(
            """SELECT COALESCE(SUM(ol.qty),0) q FROM order_lines ol JOIN orders o ON o.id=ol.order_id
               WHERE ol.item_id=? AND o.id<>? AND o.status IN ('approved','invoiced')""",
            (ln["item_id"], order_id)).fetchone()["q"]
        available = max(on_hand - reserved, 0)
        short = max(ln["qty"] - available, 0)
        bom = db.execute("SELECT yield_packets FROM boms WHERE item_id=?", (ln["item_id"],)).fetchone()
        out.append({"line_id": ln["id"], "item_id": ln["item_id"], "name": ln["name"], "ordered": ln["qty"],
                    "on_hand": on_hand, "reserved": reserved, "available": available, "short": short,
                    "ok": short == 0,
                    "batches_needed": ceil(short / bom["yield_packets"]) if (short and bom) else 0})
    return out


def update_order_lines(db, order_id, edits, user):
    """edits: {line_id: (qty_packets, price_paise)}; qty 0 removes the line. Only while 'submitted'."""
    with tx(db):
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] != "submitted":
            raise BusinessError("Only submitted orders can be edited")
        before = {l["id"]: (l["qty"], l["price_paise"]) for l in order_lines(db, order_id)}
        for lid, (qty, price) in edits.items():
            if lid not in before:
                raise BusinessError("Line does not belong to this order")
            if price < 0 or qty < 0:
                raise BusinessError("Negative values are not allowed")
            if qty == 0:
                db.execute("DELETE FROM order_lines WHERE id=?", (lid,))
            else:
                db.execute("UPDATE order_lines SET qty=?, price_paise=? WHERE id=?", (qty, price, lid))
        if not order_lines(db, order_id):
            raise BusinessError("An order must keep at least one line (reject it instead)")
        db.execute("UPDATE orders SET updated_at=? WHERE id=?", (now_ts(), order_id))
        audit(db, user, "order", order_id, "edit_lines",
              {"before": before, "after": {k: list(v) for k, v in edits.items()}})


def approve_order(db, order_id, user, allow_shortage=False):
    with tx(db):
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] != "submitted":
            raise BusinessError("Only submitted orders can be approved")
        short = [c for c in stock_check(db, order_id) if not c["ok"]]
        if short and not allow_shortage:
            raise ShortageError(short)
        db.execute("UPDATE orders SET status='approved', updated_at=? WHERE id=?", (now_ts(), order_id))
        audit(db, user, "order", order_id, "approve",
              {"shortage_override": bool(short), "shortages": [{k: c[k] for k in ("name", "short")} for c in short]})


def reject_order(db, order_id, reason, user):
    if not reason:
        raise BusinessError("Give a reason for rejecting the order")
    with tx(db):
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] not in ("submitted", "approved"):
            raise BusinessError("Only submitted or approved (un-invoiced) orders can be rejected")
        db.execute("UPDATE orders SET status='rejected', reject_reason=?, updated_at=? WHERE id=?",
                   (reason, now_ts(), order_id))
        audit(db, user, "order", order_id, "reject", {"reason": reason})


# ---- invoicing --------------------------------------------------------------
def compute_tax(db, order_id):
    """Tax for an order as it would be invoiced: GST split by place of supply, per line, half-up per paisa."""
    order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    cust = db.execute("SELECT * FROM customers WHERE id=?", (order["customer_id"],)).fetchone()
    seller_state = setting(db, "seller_state_code", "33")
    pos = cust["state_code"] or seller_state
    intra = pos == seller_state
    lines, totals = [], dict(taxable=0, cgst=0, sgst=0, igst=0)
    for ol in order_lines(db, order_id):
        item = get_item(db, ol["item_id"])
        taxable = ol["qty"] * ol["price_paise"]
        cgst, sgst, igst = line_tax(taxable, item["gst_rate_bp"], intra)
        lines.append((item, ol, taxable, cgst, sgst, igst))
        for k, v in zip(("taxable", "cgst", "sgst", "igst"), (taxable, cgst, sgst, igst)):
            totals[k] += v
    return {"customer": cust, "pos": pos, "intra": intra, "supply_type": "intra" if intra else "inter",
            "lines": lines, "total": sum(totals.values()), **totals}


def create_invoice(db, order_id, user, inv_date=None):
    inv_date = inv_date or today()
    with tx(db):
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order or order["status"] != "approved":
            raise BusinessError("Only approved orders can be invoiced")
        t = compute_tax(db, order_id)
        cust, lines, totals, total = t["customer"], t["lines"], t, t["total"]
        fy = fy_of(inv_date)
        number = f"INV/{fy}/{next_no(db, 'inv:' + fy):04d}"
        cur = db.execute(
            """INSERT INTO invoices(invoice_no, fy, order_id, customer_id, inv_date, place_of_supply, supply_type,
                  taxable_paise, cgst_paise, sgst_paise, igst_paise, total_paise, status, share_token, created_by,
                  created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (number, fy, order_id, cust["id"], inv_date, t["pos"], t["supply_type"], t["taxable"],
             t["cgst"], t["sgst"], t["igst"], total, "posted", secrets.token_urlsafe(16),
             user["id"], now_ts()))
        inv_id = cur.lastrowid
        for item, ol, taxable, cgst, sgst, igst in lines:
            db.execute(
                """INSERT INTO invoice_lines(invoice_id,item_id,description,hsn,qty,rate_paise,taxable_paise,
                      gst_rate_bp,cgst_paise,sgst_paise,igst_paise,total_paise) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (inv_id, item["id"], item["name"], item["hsn"], ol["qty"], ol["price_paise"], taxable,
                 item["gst_rate_bp"], cgst, sgst, igst, taxable + cgst + sgst + igst))
        db.execute("UPDATE orders SET status='invoiced', updated_at=? WHERE id=?", (now_ts(), order_id))
        audit(db, user, "invoice", inv_id, "create",
              {"invoice_no": number, "total_paise": total, "taxable": t["taxable"], "cgst": t["cgst"],
               "sgst": t["sgst"], "igst": t["igst"]})
    return inv_id


def get_invoice(db, invoice_id):
    inv = db.execute(
        """SELECT v.*, c.name customer_name, c.gstin customer_gstin, c.address customer_address, c.type customer_type,
                  o.order_no FROM invoices v JOIN customers c ON c.id=v.customer_id
                  LEFT JOIN orders o ON o.id=v.order_id WHERE v.id=?""", (invoice_id,)).fetchone()
    if not inv:
        raise BusinessError("Invoice not found")
    return inv


def invoice_lines(db, invoice_id):
    return db.execute("SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()


def paid_amount(db, invoice_id) -> int:
    return db.execute("SELECT COALESCE(SUM(amount_paise),0) p FROM payments WHERE invoice_id=?",
                      (invoice_id,)).fetchone()["p"]


def outstanding(db, invoice_id) -> int:
    inv = get_invoice(db, invoice_id)
    if inv["status"] == "cancelled":
        return 0
    return inv["total_paise"] - paid_amount(db, invoice_id)


def dispatch(db, invoice_id, user, on_date=None):
    """Dispatch the full invoice: finished goods leave stock (ledger SALE, valued at WAC)."""
    with tx(db):
        inv = get_invoice(db, invoice_id)
        if inv["status"] != "posted":
            raise BusinessError("Cancelled invoices cannot be dispatched")
        if inv["dispatched_at"]:
            raise BusinessError("Already dispatched")
        cogs = 0
        for ln in invoice_lines(db, invoice_id):
            cogs += stock.issue(db, ln["item_id"], ln["qty"], mtype="SALE", ref_type="invoice", ref_id=invoice_id,
                                ref_no=inv["invoice_no"], user=user, on_date=on_date or today())
        db.execute("UPDATE invoices SET dispatched_at=? WHERE id=?", (now_ts(), invoice_id))
        db.execute("UPDATE orders SET status='dispatched', updated_at=? WHERE id=?", (now_ts(), inv["order_id"]))
        audit(db, user, "invoice", invoice_id, "dispatch", {"cogs_paise": cogs})


def record_payment(db, invoice_id, amount_paise, mode, user, ref=None, pay_date=None):
    if amount_paise <= 0:
        raise BusinessError("Payment must be positive")
    with tx(db):
        inv = get_invoice(db, invoice_id)
        if inv["status"] != "posted":
            raise BusinessError("Cannot take payment against a cancelled invoice")
        due = inv["total_paise"] - paid_amount(db, invoice_id)
        if amount_paise > due:
            raise BusinessError(f"Payment exceeds the outstanding amount of {due / 100:.2f}")
        n = next_no(db, "receipt")
        cur = db.execute(
            """INSERT INTO payments(receipt_no,invoice_id,customer_id,pay_date,amount_paise,mode,ref,created_by,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (f"RCT-{n:04d}", invoice_id, inv["customer_id"], pay_date or today(), amount_paise, mode or "cash", ref,
             user["id"], now_ts()))
        audit(db, user, "payment", cur.lastrowid, "create", {"invoice_id": invoice_id, "amount_paise": amount_paise})
    return cur.lastrowid


def cancel_invoice(db, invoice_id, reason, user, goods_returned=False, cn_date=None):
    """Posted invoices are never deleted: cancel with a full credit note."""
    if not reason:
        raise BusinessError("Give a reason for the credit note")
    with tx(db):
        inv = get_invoice(db, invoice_id)
        if inv["status"] != "posted":
            raise BusinessError("Invoice is already cancelled")
        if paid_amount(db, invoice_id):
            raise BusinessError("Payments exist against this invoice; refund/adjust them before cancelling")
        cn_date = cn_date or today()
        fy = fy_of(cn_date)
        cn_no = f"CN/{fy}/{next_no(db, 'cn:' + fy):04d}"
        db.execute(
            """INSERT INTO credit_notes(cn_no,invoice_id,cn_date,taxable_paise,cgst_paise,sgst_paise,igst_paise,
                  total_paise,reason,goods_returned,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cn_no, invoice_id, cn_date, inv["taxable_paise"], inv["cgst_paise"], inv["sgst_paise"], inv["igst_paise"],
             inv["total_paise"], reason, 1 if goods_returned else 0, user["id"], now_ts()))
        returned = 0
        if inv["dispatched_at"] and goods_returned:
            returned = stock.reverse(db, "invoice", invoice_id, user, mtype="SALE_RETURN", note=f"{cn_no}: {reason}")
        db.execute("UPDATE invoices SET status='cancelled' WHERE id=?", (invoice_id,))
        db.execute("UPDATE orders SET status='cancelled', updated_at=? WHERE id=?", (now_ts(), inv["order_id"]))
        audit(db, user, "invoice", invoice_id, "cancel",
              {"cn_no": cn_no, "reason": reason, "goods_returned": goods_returned, "stock_rows_reversed": returned})
    return cn_no


# ---- salesman views ------------------------------------------------------------
def customer_wise_sales(db, salesman_id):
    return db.execute(
        """SELECT c.name, COUNT(DISTINCT o.id) orders, COALESCE(SUM(ol.qty*ol.price_paise),0) value
           FROM orders o JOIN customers c ON c.id=o.customer_id JOIN order_lines ol ON ol.order_id=o.id
           WHERE o.salesman_id=? AND o.status NOT IN ('rejected','cancelled')
           GROUP BY c.id ORDER BY value DESC""", (salesman_id,)).fetchall()
