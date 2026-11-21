from app.benchmark import run_benchmark


def test_section5_benchmark_all_checks_pass(db):
    checks, _ = run_benchmark(db)
    failed = [c for c in checks if not c[3]]
    assert not failed, failed
    assert len(checks) >= 20


def test_every_ledger_row_traces_to_a_source_and_reconciles(db):
    run_benchmark(db)
    rows = db.execute("SELECT * FROM stock_ledger").fetchall()
    assert all(r["ref_type"] and r["ref_no"] for r in rows)
    for it in db.execute("SELECT id FROM items"):
        opening_and_moves = db.execute("SELECT COALESCE(SUM(qty_base),0) FROM stock_ledger WHERE item_id=?", (it["id"],)).fetchone()[0]
        assert opening_and_moves >= 0
