import pytest

from app.money import rupees_to_paise as rp
from app.services import purchase, stock
from app.services.common import InsufficientStock


def item(db, code):
    return db.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone()


def test_balance_is_sum_of_ledger_and_wac_after_purchase(db, actors):
    flour = item(db, "FLOUR")["id"]
    assert stock.balance(db, flour) == (100000, 420000)
    sup = db.execute("SELECT id FROM suppliers").fetchone()["id"]
    purchase.create_purchase(db, sup, [{"item_id": flour, "qty": 60, "unit": "kg", "rate_paise": rp(45)}], actors["production"])
    q, v = stock.balance(db, flour)
    assert (q, v) == (160000, 690000)
    assert stock.wac_value(db, flour, 40000) == 172500      # 40 kg at Rs 43.125/kg
    # balance equals the arithmetic sum of ledger rows
    assert q == db.execute("SELECT SUM(qty_base) FROM stock_ledger WHERE item_id=?", (flour,)).fetchone()[0]


def test_cannot_issue_more_than_on_hand(db, actors):
    flour = item(db, "FLOUR")["id"]
    with pytest.raises(InsufficientStock):
        stock.issue(db, flour, 100001, mtype="PROD_ISSUE", ref_type="run", user=actors["production"])
    assert stock.balance(db, flour)[0] == 100000            # nothing partially posted


def test_issuing_everything_clears_value_exactly(db, actors):
    flour = item(db, "FLOUR")["id"]
    stock.issue(db, flour, 33333, mtype="PROD_ISSUE", ref_type="run", user=actors["production"])
    stock.issue(db, flour, 66667, mtype="PROD_ISSUE", ref_type="run", user=actors["production"])
    assert stock.balance(db, flour) == (0, 0)               # no rounding residue left behind


def test_ledger_is_append_only(db):
    import sqlite3
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE stock_ledger SET qty_base=1 WHERE id=1")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM stock_ledger WHERE id=1")


def test_adjustment_needs_reason_and_is_in_ledger(db, actors):
    from app.services.common import BusinessError
    flour = item(db, "FLOUR")["id"]
    with pytest.raises(BusinessError):
        stock.adjust(db, flour, -1000, actors["admin"], "")
    stock.adjust(db, flour, -1000, actors["admin"], "spillage")
    assert stock.balance(db, flour)[0] == 99000
    assert db.execute("SELECT COUNT(*) FROM stock_ledger WHERE mtype='ADJUSTMENT'").fetchone()[0] == 1
