"""BOM-driven production: runs, issues, completion, variance and costing."""
from datetime import date, timedelta
from decimal import Decimal

from ..db import tx
from ..money import D, rnd
from . import stock
from .common import BusinessError, audit, get_item, next_no, now_ts, setting, today

QTY2 = Decimal("0.01")


def variance(planned, actual, tol):
    """(pct, flagged). pct = (actual-planned)/planned*100. Flag if |pct| > tolerance."""
    planned, actual = D(planned), D(actual)
    if planned == 0:
        return (None, actual != 0)
    pct = (actual - planned) * 100 / planned
    return pct.quantize(QTY2), abs(pct) > D(tol)


def tolerance(db) -> Decimal:
    return D(setting(db, "variance_tolerance_pct", "3"))


def start_run(db, item_id, batches, user, run_date=None, notes=None):
    batches = D(batches)
    if batches <= 0:
        raise BusinessError("Number of batches must be positive")
    item = get_item(db, item_id)
    bom = db.execute("SELECT * FROM boms WHERE item_id=?", (item_id,)).fetchone()
    if not bom:
        raise BusinessError(f"No BOM defined for {item['name']}")
    lines = db.execute("SELECT * FROM bom_lines WHERE bom_id=?", (bom["id"],)).fetchall()
    if not lines:
        raise BusinessError("BOM has no lines")
    run_date = run_date or today()
    with tx(db):
        n = next_no(db, "run")
        run_no = f"RUN-{n:04d}"
        batch_no = f"B{run_date.replace('-', '')[2:]}-{n:03d}"
        expiry = None
        if item["shelf_life_days"]:
            expiry = (date.fromisoformat(run_date) + timedelta(days=item["shelf_life_days"])).isoformat()
        expected = rnd(D(bom["yield_packets"]) * batches)
        cur = db.execute(
            """INSERT INTO runs(run_no,item_id,bom_id,run_date,status,batches,expected_output,batch_no,expiry,notes,
                                created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_no, item_id, bom["id"], run_date, "open", str(batches.normalize()), expected, batch_no, expiry,
             notes, user["id"], now_ts()))
        run_id = cur.lastrowid
        for ln in lines:
            db.execute("INSERT INTO run_lines(run_id,item_id,planned_base,per_packet) VALUES (?,?,?,?)",
                       (run_id, ln["item_id"], rnd(D(ln["qty_base"]) * batches), ln["per_packet"]))
        audit(db, user, "run", run_id, "start", {"run_no": run_no, "batches": str(batches), "expected": expected})
    return run_id


def _open_run(db, run_id):
    run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise BusinessError("Run not found")
    if run["status"] != "open":
        raise BusinessError(f"{run['run_no']} is {run['status']}; it can no longer take issues")
    return run


def issue_material(db, run_id, item_id, qty_base, user, on_date=None):
    """Issue raw/packaging stock to a run (ledger PROD_ISSUE at WAC)."""
    if qty_base <= 0:
        raise BusinessError("Issue quantity must be positive")
    with tx(db):
        run = _open_run(db, run_id)
        line = db.execute("SELECT * FROM run_lines WHERE run_id=? AND item_id=?", (run_id, item_id)).fetchone()
        if not line:
            raise BusinessError("That item is not in this product's BOM")
        value = stock.issue(db, item_id, qty_base, mtype="PROD_ISSUE", ref_type="run", ref_id=run_id,
                            ref_no=run["run_no"], user=user, on_date=on_date or run["run_date"],
                            batch_no=run["batch_no"])
        db.execute("UPDATE run_lines SET actual_base=actual_base+?, actual_value_paise=actual_value_paise+? WHERE id=?",
                   (qty_base, value, line["id"]))
        audit(db, user, "run", run_id, "issue", {"item_id": item_id, "qty_base": qty_base, "value_paise": value})
    return value


def complete_run(db, run_id, good_output, damaged_units, user, packaging_totals=None, on_date=None):
    """Record actual output and wastage; consume packaging; add finished goods to stock.

    packaging_totals: {item_id: TOTAL base qty actually used}. Lines flagged per_packet
    (pouches) default to good + damaged. Any shortfall vs what is already issued is issued now.
    """
    good_output, damaged_units = int(good_output), int(damaged_units)
    if good_output <= 0:
        raise BusinessError("Good output must be greater than zero (cancel the run if nothing was produced)")
    if damaged_units < 0:
        raise BusinessError("Damaged units cannot be negative")
    targets = dict(packaging_totals or {})
    with tx(db):
        run = _open_run(db, run_id)
        on_date = on_date or run["run_date"]
        lines = db.execute("SELECT * FROM run_lines WHERE run_id=?", (run_id,)).fetchall()
        for ln in lines:
            if ln["per_packet"] and ln["item_id"] not in targets:
                targets[ln["item_id"]] = good_output + damaged_units
        for item_id, target in targets.items():
            ln = next((l for l in lines if l["item_id"] == item_id), None)
            if not ln:
                raise BusinessError("Item is not in this product's BOM")
            delta = int(target) - ln["actual_base"]
            if delta < 0:
                raise BusinessError(
                    f"{get_item(db, item_id)['name']}: {ln['actual_base']} already issued, more than the {target} entered")
            if delta > 0:
                issue_material(db, run_id, item_id, delta, user, on_date)
        materials = db.execute("SELECT COALESCE(SUM(actual_value_paise),0) v FROM run_lines WHERE run_id=?",
                               (run_id,)).fetchone()["v"]
        overhead = db.execute("SELECT COALESCE(SUM(amount_paise),0) v FROM run_overheads WHERE run_id=?",
                              (run_id,)).fetchone()["v"]
        stock.receive(db, run["item_id"], good_output, materials + overhead, mtype="PROD_OUTPUT", ref_type="run",
                      ref_id=run_id, ref_no=run["run_no"], user=user, on_date=on_date, batch_no=run["batch_no"],
                      expiry=run["expiry"])
        db.execute("UPDATE runs SET status='completed', good_output=?, damaged_units=?, completed_at=? WHERE id=?",
                   (good_output, damaged_units, now_ts(), run_id))
        audit(db, user, "run", run_id, "complete",
              {"good": good_output, "damaged": damaged_units, "expected": run["expected_output"],
               "materials_paise": materials, "overhead_paise": overhead})


def add_overhead(db, run_id, kind, amount_paise, user, note=None):
    if amount_paise <= 0:
        raise BusinessError("Overhead amount must be positive")
    with tx(db):
        run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] == "cancelled":
            raise BusinessError("Run not found or cancelled")
        db.execute("INSERT INTO run_overheads(run_id,kind,amount_paise,note,created_by,created_at) VALUES (?,?,?,?,?,?)",
                   (run_id, kind, amount_paise, note, user["id"], now_ts()))
        if run["status"] == "completed":
            # capitalise into finished-goods value: a value-only ledger movement (qty 0)
            stock.post(db, item_id=run["item_id"], qty=0, value=amount_paise, mtype="COST_ADJ", ref_type="run",
                       ref_id=run_id, ref_no=run["run_no"], user=user, batch_no=run["batch_no"],
                       note=f"{kind} overhead")
        audit(db, user, "run", run_id, "overhead", {"kind": kind, "amount_paise": amount_paise})


def cancel_run(db, run_id, reason, user):
    if not reason:
        raise BusinessError("Give a reason for cancelling the run")
    with tx(db):
        run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run or run["status"] == "cancelled":
            raise BusinessError("Run not found or already cancelled")
        stock.reverse(db, "run", run_id, user, note=f"run cancelled: {reason}")
        db.execute("UPDATE runs SET status='cancelled' WHERE id=?", (run_id,))
        audit(db, user, "run", run_id, "cancel", {"reason": reason})


# ---- reporting helpers ---------------------------------------------------
def run_summary(db, run_id):
    run = db.execute("SELECT r.*, i.name product, i.price_paise FROM runs r JOIN items i ON i.id=r.item_id WHERE r.id=?",
                     (run_id,)).fetchone()
    if not run:
        raise BusinessError("Run not found")
    tol = tolerance(db)
    lines = []
    for ln in db.execute(
            """SELECT rl.*, i.name, i.kind, i.disp_unit, i.factor FROM run_lines rl JOIN items i ON i.id=rl.item_id
               WHERE rl.run_id=? ORDER BY i.kind DESC, i.name""", (run_id,)):
        pct, flag = variance(ln["planned_base"], ln["actual_base"], tol)
        if run["status"] == "open" and ln["actual_base"] == 0:
            pct, flag = None, False  # not issued yet
        lines.append({**dict(ln), "var_base": ln["actual_base"] - ln["planned_base"], "var_pct": pct, "flag": flag})
    overheads = db.execute("SELECT * FROM run_overheads WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    out = {"run": run, "lines": lines, "overheads": overheads, "tolerance": tol}
    materials = sum(l["actual_value_paise"] for l in lines)
    oh = sum(o["amount_paise"] for o in overheads)
    out.update(materials_paise=materials, overhead_paise=oh, total_paise=materials + oh)
    if run["status"] == "completed":
        pct, flag = variance(run["expected_output"], run["good_output"], tol)
        out["yield_pct"], out["yield_flag"] = pct, flag
        out["yield_loss"] = max(run["expected_output"] - run["good_output"], 0)
        out["cost_per_packet"] = D(out["total_paise"]) / D(run["good_output"])  # paise, Decimal
        out["margin_per_packet"] = D(run["price_paise"]) - out["cost_per_packet"]
        out["margin_pct"] = (out["margin_per_packet"] * 100 / D(run["price_paise"])) if run["price_paise"] else None
    else:
        out["yield_pct"] = out["yield_flag"] = out["cost_per_packet"] = None
    out["alerts"] = [l["name"] for l in lines if l["flag"]] + (["Yield"] if out.get("yield_flag") else [])
    return out


def variance_alerts(db, limit=20):
    """Flagged variances across completed runs (for the owner dashboard)."""
    alerts = []
    for r in db.execute("SELECT id FROM runs WHERE status='completed' ORDER BY id DESC LIMIT ?", (limit,)):
        s = run_summary(db, r["id"])
        for l in s["lines"]:
            if l["flag"]:
                alerts.append({"run_no": s["run"]["run_no"], "what": l["name"], "pct": l["var_pct"]})
        if s["yield_flag"]:
            alerts.append({"run_no": s["run"]["run_no"], "what": "Yield", "pct": s["yield_pct"]})
    return alerts
