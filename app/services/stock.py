"""Stock ledger: the only way stock changes.

Every movement is an append-only row (qty and value, signed) linked to its source
document. Balances are SUM()s. Valuation is moving weighted-average cost (WAC):
an issue is valued at (current total value / current total qty) x qty issued.
"""
from decimal import Decimal

from ..money import D, rnd, fmt_qty
from .common import InsufficientStock, BusinessError, now_ts, today, get_item


def balance(db, item_id):
    r = db.execute(
        "SELECT COALESCE(SUM(qty_base),0) q, COALESCE(SUM(value_paise),0) v FROM stock_ledger WHERE item_id=?",
        (item_id,),
    ).fetchone()
    return int(r["q"]), int(r["v"])


def post(db, *, item_id, qty, value, mtype, ref_type, ref_id=None, ref_no=None, user=None,
         on_date=None, batch_no=None, expiry=None, note=None, reverses_id=None):
    """Append one ledger row. Refuses to take stock negative."""
    if qty < 0:
        bal, _ = balance(db, item_id)
        if bal + qty < 0:
            item = get_item(db, item_id)
            raise InsufficientStock(item["name"], fmt_qty(-qty, item, False), fmt_qty(bal, item, False), item["disp_unit"])
    cur = db.execute(
        """INSERT INTO stock_ledger(ts, mov_date, item_id, qty_base, value_paise, mtype, ref_type, ref_id, ref_no,
                                    batch_no, expiry, reverses_id, user_id, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now_ts(), on_date or today(), item_id, qty, value, mtype, ref_type, ref_id, ref_no,
         batch_no, expiry, reverses_id, user["id"] if user else None, note),
    )
    return cur.lastrowid


def wac_value(db, item_id, qty) -> int:
    """Value (paise) of issuing `qty` base units at the current weighted-average cost."""
    bal, val = balance(db, item_id)
    if qty > bal:
        item = get_item(db, item_id)
        raise InsufficientStock(item["name"], fmt_qty(qty, item, False), fmt_qty(bal, item, False), item["disp_unit"])
    if qty == bal:
        return val  # clears the stock exactly, no rounding residue
    return rnd(Decimal(val) * qty / bal)


def issue(db, item_id, qty, **kw) -> int:
    value = wac_value(db, item_id, qty)
    post(db, item_id=item_id, qty=-qty, value=-value, **kw)
    return value


def receive(db, item_id, qty, value, **kw):
    return post(db, item_id=item_id, qty=qty, value=value, **kw)


def reverse(db, ref_type, ref_id, user, mtype="REVERSAL", note=None):
    """Post equal-and-opposite rows for every un-reversed movement of a source document."""
    rows = db.execute(
        """SELECT * FROM stock_ledger l WHERE ref_type=? AND ref_id=? AND mtype NOT IN ('REVERSAL','SALE_RETURN')
             AND NOT EXISTS (SELECT 1 FROM stock_ledger r WHERE r.reverses_id = l.id)
           ORDER BY id""", (ref_type, ref_id)).fetchall()
    for r in rows:
        post(db, item_id=r["item_id"], qty=-r["qty_base"], value=-r["value_paise"], mtype=mtype,
             ref_type=ref_type, ref_id=ref_id, ref_no=r["ref_no"], user=user, batch_no=r["batch_no"],
             expiry=r["expiry"], reverses_id=r["id"], note=note)
    return len(rows)


def adjust(db, item_id, qty, user, note, on_date=None):
    """Manual stock adjustment (count difference, spillage). Valued at WAC."""
    if not note:
        raise BusinessError("An adjustment needs a reason")
    if qty < 0:
        return issue(db, item_id, -qty, mtype="ADJUSTMENT", ref_type="adjustment", user=user, note=note, on_date=on_date)
    bal, val = balance(db, item_id)
    unit_val = rnd(Decimal(val) * qty / bal) if bal > 0 else 0
    post(db, item_id=item_id, qty=qty, value=unit_val, mtype="ADJUSTMENT", ref_type="adjustment",
         user=user, note=note, on_date=on_date)
    return unit_val


def stock_levels(db, kind=None):
    sql = """SELECT i.*, COALESCE(SUM(l.qty_base),0) qty, COALESCE(SUM(l.value_paise),0) value
             FROM items i LEFT JOIN stock_ledger l ON l.item_id=i.id WHERE i.active=1"""
    args = []
    if kind:
        sql += " AND i.kind=?"
        args.append(kind)
    sql += " GROUP BY i.id ORDER BY i.kind, i.name"
    return db.execute(sql, args).fetchall()
