"""Runs the Section 5 benchmark end to end through the service layer and checks every expected figure.

    python -m app.benchmark            # on a throw-away in-memory database
The same function is used by tests/test_benchmark.py.
"""
from decimal import Decimal

from . import db as dbm
from .money import D, inr, rupees_to_paise as rp
from .seed import seed
from .services import production as prod, purchase, sales, stock


def _actor(db, username):
    return dict(db.execute("SELECT id, username, role FROM users WHERE username=?", (username,)).fetchone())


def _item(db, code):
    return db.execute("SELECT * FROM items WHERE code=?", (code,)).fetchone()


def _qty(db, code):
    return stock.balance(db, _item(db, code)["id"])[0]


def run_benchmark(db, verbose=False):
    """Returns (checks, ctx). checks = [(name, expected, actual, ok)]."""
    admin, production = _actor(db, "admin"), _actor(db, "production")
    sa, sb = _actor(db, "salesA"), _actor(db, "salesB")
    mur = _item(db, "MUR200")
    ret_a = db.execute("SELECT id FROM customers WHERE name='Retailer A'").fetchone()["id"]
    dist_b = db.execute("SELECT id FROM customers WHERE name='Distributor B'").fetchone()["id"]
    sup = db.execute("SELECT id FROM suppliers").fetchone()["id"]
    checks = []

    def check(name, expected, actual):
        checks.append((name, expected, actual, expected == actual))

    # 1. purchase 60 kg flour @ 45
    purchase.create_purchase(db, sup, [{"item_id": _item(db, "FLOUR")["id"], "qty": 60, "unit": "kg", "rate_paise": rp(45)}],
                             production, supplier_inv_no="SLF/101")
    # 2. production run: issue 40 kg flour, 10.5 L oil, 1.2 kg spice
    run_id = prod.start_run(db, mur["id"], 1, production)
    for code, qty_base in (("FLOUR", 40_000), ("OIL", 10_500), ("SPICE", 1_200)):
        prod.issue_material(db, run_id, _item(db, code)["id"], qty_base, production)
    # 3. actual output 188, 6 pouches damaged, 9 cartons used
    prod.complete_run(db, run_id, 188, 6, production, packaging_totals={_item(db, "CARTON20")["id"]: 9})
    s = prod.run_summary(db, run_id)
    check("Flour closing balance (kg)", Decimal(120), D(_qty(db, "FLOUR")) / 1000)
    check("Oil closing balance (L)", Decimal("19.5"), D(_qty(db, "OIL")) / 1000)
    check("Pouches closing balance", 306, _qty(db, "POUCH200"))
    check("Finished goods after run", 238, _qty(db, "MUR200"))
    check("Yield variance %", Decimal("-6.00"), s["yield_pct"])
    check("Yield flagged", True, s["yield_flag"])
    oil = next(l for l in s["lines"] if l["name"] == "Cooking oil")
    check("Oil consumption variance %", Decimal("5.00"), oil["var_pct"])
    check("Oil variance flagged", True, oil["flag"])

    # 4. Salesman A (mobile) orders 150 for Retailer A
    oid_a, _ = sales.create_order(db, ret_a, sa, [{"item_id": mur["id"], "qty": 150, "unit": "packet"}], client_uuid="bench-A")
    # 5. admin approves (stock check), invoices, dispatches
    assert all(c["ok"] for c in sales.stock_check(db, oid_a))
    sales.approve_order(db, oid_a, admin)
    inv_a = sales.create_invoice(db, oid_a, admin)
    sales.dispatch(db, inv_a, admin)
    inv = sales.get_invoice(db, inv_a)
    check("Finished goods after Retailer A dispatch", 88, _qty(db, "MUR200"))
    check("Retailer A taxable (paise)", rp(7500), inv["taxable_paise"])
    check("Retailer A CGST (paise)", rp("187.50"), inv["cgst_paise"])
    check("Retailer A SGST (paise)", rp("187.50"), inv["sgst_paise"])
    check("Retailer A IGST (paise)", 0, inv["igst_paise"])
    check("Retailer A invoice total (paise)", rp(7875), inv["total_paise"])

    # 6. Salesman B orders 120 for Distributor B (Karnataka) - only 88 in stock
    oid_b, _ = sales.create_order(db, dist_b, sb, [{"item_id": mur["id"], "qty": 120, "unit": "packet"}], client_uuid="bench-B")
    chk = sales.stock_check(db, oid_b)[0]
    check("Distributor B shortage (packets)", 32, chk["short"])
    blocked = False
    try:
        sales.approve_order(db, oid_b, admin)
    except sales.BusinessError:
        blocked = True
    check("Approval blocked while stock is short", True, blocked)
    t = sales.compute_tax(db, oid_b)
    check("Distributor B would be taxed as IGST", "inter", t["supply_type"])
    check("Distributor B IGST if invoiced (paise)", rp(300), t["igst"])

    # 7. payment Rs 4,000 from Retailer A
    sales.record_payment(db, inv_a, rp(4000), "upi", admin, ref="UPI123")
    check("Retailer A outstanding (paise)", rp(3875), sales.outstanding(db, inv_a))

    # 8. labour + power/gas overheads, then cost per packet
    prod.add_overhead(db, run_id, "labour", rp(1200), production)
    prod.add_overhead(db, run_id, "power", rp(600), production)
    s = prod.run_summary(db, run_id)
    check("Run material cost (paise)", rp(4055), s["materials_paise"])
    check("Run total cost incl. overheads (paise)", rp(5855), s["total_paise"])
    check("Cost per packet (Rs, 2 dp)", Decimal("31.14"), (s["cost_per_packet"] / 100).quantize(Decimal("0.01")))
    check("Margin per packet (Rs, 2 dp)", Decimal("18.86"), (s["margin_per_packet"] / 100).quantize(Decimal("0.01")))
    return checks, {"run_id": run_id, "inv_a": inv_a, "order_b": oid_b}


def main():
    db = dbm.connect(":memory:")
    dbm.init_schema(db)
    seed(db)
    checks, _ = run_benchmark(db)
    w = max(len(c[0]) for c in checks)
    for name, exp, act, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{w}}  expected={exp}  actual={act}")
    bad = [c for c in checks if not c[3]]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} checks passed")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
