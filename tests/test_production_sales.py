import sqlite3
from decimal import Decimal

import pytest

from app.money import rupees_to_paise as rp
from app.services import production as prod, sales, stock
from app.services.common import BusinessError, ShortageError, InsufficientStock


def code(db, c):
    return db.execute("SELECT * FROM items WHERE code=?", (c,)).fetchone()


def make_run(db, actors, good=188, damaged=6, cartons=9, oil=10_500):
    mur = code(db, "MUR200")["id"]
    rid = prod.start_run(db, mur, 1, actors["production"])
    for c, q in (("FLOUR", 40_000), ("OIL", oil), ("SPICE", 1_200)):
        prod.issue_material(db, rid, code(db, c)["id"], q, actors["production"])
    prod.complete_run(db, rid, good, damaged, actors["production"], packaging_totals={code(db, "CARTON20")["id"]: cartons})
    return rid


def test_variance_flags_both_directions_only_beyond_tolerance(db, actors):
    s = prod.run_summary(db, make_run(db, actors))
    by = {l["name"]: l for l in s["lines"]}
    assert by["Cooking oil"]["flag"] and by["Cooking oil"]["var_pct"] == Decimal("5.00")
    assert not by["200 g pouch"]["flag"]                   # exactly -3.00% is within a 3% tolerance
    assert by["Carton (holds 20 packets)"]["flag"]         # -10%: under-use is flagged too
    assert s["yield_flag"] and s["yield_pct"] == Decimal("-6.00")


def test_tolerance_is_configurable(db, actors):
    db.execute("UPDATE settings SET value='10' WHERE key='variance_tolerance_pct'")
    s = prod.run_summary(db, make_run(db, actors))
    assert not s["yield_flag"] and not any(l["flag"] for l in s["lines"] if l["name"] == "Cooking oil")


def test_run_cannot_issue_item_outside_bom_or_after_completion(db, actors):
    rid = make_run(db, actors)
    with pytest.raises(BusinessError):
        prod.issue_material(db, rid, code(db, "FLOUR")["id"], 1000, actors["production"])   # completed
    rid2 = prod.start_run(db, code(db, "MUR200")["id"], 1, actors["production"])
    with pytest.raises(BusinessError):
        prod.issue_material(db, rid2, code(db, "MUR200")["id"], 1, actors["production"])    # not in BOM


def test_multi_batch_scales_plan_and_fractional_batches(db, actors):
    rid = prod.start_run(db, code(db, "MUR200")["id"], "1.5", actors["production"])
    s = prod.run_summary(db, rid)
    assert s["run"]["expected_output"] == 300
    assert {l["name"]: l["planned_base"] for l in s["lines"]}["Flour"] == 60_000


def test_overhead_after_completion_raises_finished_goods_value_not_qty(db, actors):
    rid = make_run(db, actors)
    mur = code(db, "MUR200")["id"]
    q0, v0 = stock.balance(db, mur)
    prod.add_overhead(db, rid, "labour", rp(1200), actors["production"])
    q1, v1 = stock.balance(db, mur)
    assert q1 == q0 and v1 - v0 == rp(1200)


def test_cancel_run_reverses_every_movement(db, actors):
    before = {i["id"]: stock.balance(db, i["id"]) for i in db.execute("SELECT id FROM items")}
    rid = make_run(db, actors)
    prod.cancel_run(db, rid, "wrong batch", actors["production"])
    after = {i["id"]: stock.balance(db, i["id"]) for i in db.execute("SELECT id FROM items")}
    assert before == after


def test_cancel_run_blocked_if_output_already_sold(db, actors):
    rid = make_run(db, actors)
    cust = db.execute("SELECT id FROM customers WHERE name='Retailer A'").fetchone()["id"]
    oid, _ = sales.create_order(db, cust, actors["salesA"], [{"item_id": code(db, "MUR200")["id"], "qty": 230, "unit": "packet"}])
    sales.approve_order(db, oid, actors["admin"])
    sales.dispatch(db, sales.create_invoice(db, oid, actors["admin"]), actors["admin"])
    with pytest.raises(InsufficientStock):
        prod.cancel_run(db, rid, "oops", actors["production"])
    assert db.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()["status"] == "completed"   # rolled back


def _order(db, actors, who, customer, qty, unit="packet", uuid=None):
    cust = db.execute("SELECT id FROM customers WHERE name=?", (customer,)).fetchone()["id"]
    return sales.create_order(db, cust, actors[who], [{"item_id": code(db, "MUR200")["id"], "qty": qty, "unit": unit}], client_uuid=uuid)[0]


def test_cartons_convert_to_packets_on_orders(db, actors):
    oid = _order(db, actors, "salesA", "Retailer A", 2, "carton")
    assert sales.order_lines(db, oid)[0]["qty"] == 40


def test_offline_sync_is_idempotent(db, actors):
    a = _order(db, actors, "salesA", "Retailer A", 5, uuid="same-uuid")
    b = _order(db, actors, "salesA", "Retailer A", 5, uuid="same-uuid")
    assert a == b and db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_stock_check_reserves_for_other_approved_orders(db, actors):
    o1 = _order(db, actors, "salesA", "Retailer A", 40)       # 50 opening packets
    o2 = _order(db, actors, "salesB", "Distributor B", 20)
    sales.approve_order(db, o1, actors["admin"])
    c = sales.stock_check(db, o2)[0]
    assert (c["on_hand"], c["reserved"], c["available"], c["short"]) == (50, 40, 10, 10)
    with pytest.raises(ShortageError):
        sales.approve_order(db, o2, actors["admin"])
    sales.approve_order(db, o2, actors["admin"], allow_shortage=True)   # explicit override is allowed and audited
    assert db.execute("SELECT details FROM audit_log WHERE entity='order' AND action='approve' ORDER BY id DESC").fetchone()[0].count("shortage_override") == 1


def test_dispatch_blocked_when_stock_short_and_nothing_posted(db, actors):
    oid = _order(db, actors, "salesA", "Retailer A", 60)
    sales.approve_order(db, oid, actors["admin"], allow_shortage=True)
    inv = sales.create_invoice(db, oid, actors["admin"])
    with pytest.raises(InsufficientStock):
        sales.dispatch(db, inv, actors["admin"])
    assert stock.balance(db, code(db, "MUR200")["id"])[0] == 50
    assert sales.get_invoice(db, inv)["dispatched_at"] is None


def test_interstate_invoice_uses_igst(db, actors):
    oid = _order(db, actors, "salesB", "Distributor B", 40)
    sales.approve_order(db, oid, actors["admin"])
    inv = sales.get_invoice(db, sales.create_invoice(db, oid, actors["admin"]))
    assert (inv["supply_type"], inv["cgst_paise"], inv["sgst_paise"], inv["igst_paise"]) == ("inter", 0, 0, rp(100))
    assert inv["invoice_no"] == "INV/26-27/0001" or inv["invoice_no"].startswith("INV/")


def test_invoice_numbers_are_sequential_per_financial_year(db, actors):
    nums = []
    for d in ("2026-03-30", "2026-04-02", "2026-04-03"):
        oid = _order(db, actors, "salesA", "Retailer A", 1)
        sales.approve_order(db, oid, actors["admin"])
        nums.append(sales.get_invoice(db, sales.create_invoice(db, oid, actors["admin"], inv_date=d))["invoice_no"])
    assert nums == ["INV/25-26/0001", "INV/26-27/0001", "INV/26-27/0002"]


def test_posted_invoice_cannot_be_deleted_or_altered_only_cancelled(db, actors):
    oid = _order(db, actors, "salesA", "Retailer A", 10)
    sales.approve_order(db, oid, actors["admin"])
    iid = sales.create_invoice(db, oid, actors["admin"])
    sales.dispatch(db, iid, actors["admin"])
    for sql in ("DELETE FROM invoices WHERE id=%d", "UPDATE invoices SET total_paise=1 WHERE id=%d",
                "DELETE FROM invoice_lines WHERE invoice_id=%d", "UPDATE invoice_lines SET qty=1 WHERE invoice_id=%d"):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(sql % iid)
    mur = code(db, "MUR200")["id"]
    cn = sales.cancel_invoice(db, iid, "wrong customer", actors["admin"], goods_returned=True)
    assert cn.startswith("CN/") and sales.outstanding(db, iid) == 0
    assert stock.balance(db, mur)[0] == 50                      # goods came back
    with pytest.raises(BusinessError):
        sales.cancel_invoice(db, iid, "again", actors["admin"])


def test_payments_cannot_exceed_outstanding_and_block_cancel(db, actors):
    oid = _order(db, actors, "salesA", "Retailer A", 10)
    sales.approve_order(db, oid, actors["admin"])
    iid = sales.create_invoice(db, oid, actors["admin"])          # 10 x 50 + 5% = 525.00
    with pytest.raises(BusinessError):
        sales.record_payment(db, iid, rp(525.01), "cash", actors["admin"])
    sales.record_payment(db, iid, rp(200), "cash", actors["admin"])
    assert sales.outstanding(db, iid) == rp(325)
    with pytest.raises(BusinessError):
        sales.cancel_invoice(db, iid, "x", actors["admin"])


def test_admin_edits_are_audited_with_before_and_after(db, actors):
    oid = _order(db, actors, "salesB", "Distributor B", 120)
    lid = sales.order_lines(db, oid)[0]["id"]
    sales.update_order_lines(db, oid, {lid: (88, rp(48))}, actors["admin"])
    row = db.execute("SELECT * FROM audit_log WHERE action='edit_lines'").fetchone()
    assert row["username"] == "admin" and "120" in row["details"] and "88" in row["details"]
    assert sales.order_value(db, oid) == 88 * rp(48)


def test_customer_specific_price_overrides_list_price(db, actors):
    cust = db.execute("SELECT id FROM customers WHERE name='Distributor B'").fetchone()["id"]
    db.execute("INSERT INTO customer_prices VALUES (?,?,?)", (cust, code(db, "MUR200")["id"], rp(45)))
    oid = _order(db, actors, "salesB", "Distributor B", 10)
    assert sales.order_lines(db, oid)[0]["price_paise"] == rp(45)
