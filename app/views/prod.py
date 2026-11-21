"""Production floor: runs, issues, purchases, items & BOM, stock adjustments."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from ..db import tx
from ..money import D, fmt_qty, rupees_to_paise, to_base, units_for
from ..services import production as pr
from ..services import purchase as pu
from ..services import stock
from ..services.common import BusinessError, audit
from .helpers import attempt, get_db, roles_required

bp = Blueprint("prod", __name__)
FLOOR = ("admin", "production")


# ------------------------------------------------------------ runs
@bp.route("/prod")
@roles_required(*FLOOR)
def runs():
    db = get_db()
    rows = db.execute("SELECT r.*, i.name product FROM runs r JOIN items i ON i.id=r.item_id ORDER BY r.id DESC LIMIT 100").fetchall()
    products = db.execute("SELECT i.* FROM items i JOIN boms b ON b.item_id=i.id WHERE i.active=1 ORDER BY i.name").fetchall()
    return render_template("runs.html", runs=rows, products=products)


@bp.route("/prod/runs/start", methods=["POST"])
@roles_required(*FLOOR)
def run_start():
    rid = attempt(lambda: pr.start_run(get_db(), int(request.form["item_id"]), request.form.get("batches") or "1", g.user,
                                       run_date=request.form.get("date") or None), "Run started")
    return redirect(url_for("prod.run", rid=rid) if rid else url_for("prod.runs"))


@bp.route("/prod/runs/<int:rid>")
@roles_required(*FLOOR)
def run(rid):
    db = get_db()
    try:
        s = pr.run_summary(db, rid)
    except BusinessError:
        abort(404)
    # stock on hand beside each BOM line helps the supervisor decide what to issue
    for l in s["lines"]:
        item = db.execute("SELECT * FROM items WHERE id=?", (l["item_id"],)).fetchone()
        l["item"] = item
        l["on_hand"] = stock.balance(db, l["item_id"])[0]
        l["units"] = units_for(item)
        l["remaining"] = max(l["planned_base"] - l["actual_base"], 0)
    carton = next((l for l in s["lines"] if l["kind"] == "packaging" and not l["per_packet"]), None)
    return render_template("run.html", s=s, run=s["run"], carton=carton)


@bp.route("/prod/runs/<int:rid>/issue", methods=["POST"])
@roles_required(*FLOOR)
def run_issue(rid):
    def go():
        db = get_db()
        item = db.execute("SELECT * FROM items WHERE id=?", (int(request.form["item_id"]),)).fetchone()
        pr.issue_material(db, rid, item["id"], to_base(request.form["qty"], request.form.get("unit"), item), g.user)
    attempt(go, "Material issued to run")
    return redirect(url_for("prod.run", rid=rid))


@bp.route("/prod/runs/<int:rid>/complete", methods=["POST"])
@roles_required(*FLOOR)
def run_complete(rid):
    def go():
        db = get_db()
        pack = {}
        for k, v in request.form.items():
            if k.startswith("pack_") and v.strip() != "":
                item = db.execute("SELECT * FROM items WHERE id=?", (int(k[5:]),)).fetchone()
                pack[item["id"]] = to_base(v, item["disp_unit"], item)
        pr.complete_run(db, rid, int(request.form["good"]), int(request.form.get("damaged") or 0), g.user, packaging_totals=pack)
    attempt(go, "Output recorded: raw material down, finished goods up")
    return redirect(url_for("prod.run", rid=rid))


@bp.route("/prod/runs/<int:rid>/overhead", methods=["POST"])
@roles_required(*FLOOR)
def run_overhead(rid):
    attempt(lambda: pr.add_overhead(get_db(), rid, request.form["kind"], rupees_to_paise(request.form["amount"]), g.user,
                                    note=request.form.get("note")), "Overhead added")
    return redirect(url_for("prod.run", rid=rid))


@bp.route("/prod/runs/<int:rid>/cancel", methods=["POST"])
@roles_required(*FLOOR)
def run_cancel(rid):
    attempt(lambda: pr.cancel_run(get_db(), rid, request.form.get("reason", "").strip(), g.user), "Run cancelled; stock reversed")
    return redirect(url_for("prod.run", rid=rid))


# ------------------------------------------------------------ purchases & suppliers
@bp.route("/prod/purchases")
@roles_required(*FLOOR)
def purchases():
    db = get_db()
    rows = db.execute("""SELECT p.*, s.name sname, COALESCE((SELECT SUM(amount_paise+gst_paise) FROM purchase_lines WHERE purchase_id=p.id),0) total
                         FROM purchases p LEFT JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC LIMIT 100""").fetchall()
    items = db.execute("SELECT * FROM items WHERE kind IN ('raw','packaging') AND active=1 ORDER BY kind, name").fetchall()
    sups = db.execute("SELECT * FROM suppliers ORDER BY name").fetchall()
    return render_template("purchases.html", purchases=rows, items=items, suppliers=sups,
                           units={i["id"]: units_for(i) for i in items})


@bp.route("/prod/purchases/new", methods=["POST"])
@roles_required(*FLOOR)
def purchase_new():
    def go():
        lines = []
        for n in range(1, 6):
            if request.form.get(f"qty{n}", "").strip():
                lines.append({"item_id": int(request.form[f"item{n}"]), "qty": request.form[f"qty{n}"], "unit": request.form.get(f"unit{n}"),
                              "rate_paise": rupees_to_paise(request.form[f"rate{n}"]),
                              "gst_rate_bp": int(D(request.form.get(f"gst{n}") or 0) * 100)})
        return pu.create_purchase(get_db(), int(request.form["supplier_id"]) if request.form.get("supplier_id") else None, lines,
                                  g.user, supplier_inv_no=request.form.get("inv_no"), purchase_date=request.form.get("date") or None)
    attempt(go, "Purchase recorded; stock increased")
    return redirect(url_for("prod.purchases"))


@bp.route("/prod/suppliers", methods=["POST"])
@roles_required(*FLOOR)
def supplier_new():
    def go():
        if not request.form["name"].strip():
            raise BusinessError("Name is required")
        db = get_db()
        cur = db.execute("INSERT INTO suppliers(name,gstin,state_code,phone) VALUES (?,?,?,?)",
                         (request.form["name"].strip(), request.form.get("gstin") or None, request.form.get("state_code") or None, request.form.get("phone")))
        audit(db, g.user, "supplier", cur.lastrowid, "create", dict(request.form))
    attempt(go, "Supplier added")
    return redirect(url_for("prod.purchases"))


# ------------------------------------------------------------ items & BOM
UNIT_CHOICES = {"kg": ("g", "kg", 1000), "L": ("ml", "L", 1000), "piece": ("piece", "piece", 1), "packet": ("packet", "packet", 1)}


@bp.route("/prod/items")
@roles_required(*FLOOR)
def items():
    db = get_db()
    rows = stock.stock_levels(db)
    boms = {b["item_id"]: b for b in db.execute("SELECT * FROM boms")}
    return render_template("items.html", items=rows, boms=boms)


@bp.route("/prod/items/save", methods=["POST"])
@roles_required(*FLOOR)
def item_save():
    f = request.form

    def go():
        base, disp, factor = UNIT_CHOICES[f["unit"]]
        reorder = to_base(f.get("reorder") or 0, disp, {"base_unit": base, "disp_unit": disp, "factor": factor, "carton_size": None, "name": f["name"]})
        db = get_db()
        cols = dict(code=f["code"].strip().upper(), name=f["name"].strip(), kind=f["kind"], base_unit=base, disp_unit=disp,
                    factor=factor, hsn=f.get("hsn") or None, gst_rate_bp=int(D(f.get("gst") or 0) * 100),
                    price_paise=rupees_to_paise(f.get("price") or 0), carton_size=int(f["carton"]) if f.get("carton") else None,
                    shelf_life_days=int(f["shelf"]) if f.get("shelf") else None, reorder_level_base=reorder)
        if not cols["code"] or not cols["name"]:
            raise BusinessError("Code and name are required")
        if f.get("id"):
            sets = ",".join(f"{k}=?" for k in cols)
            db.execute(f"UPDATE items SET {sets} WHERE id=?", [*cols.values(), int(f["id"])])
            audit(db, g.user, "item", int(f["id"]), "update", cols)
        else:
            cur = db.execute(f"INSERT INTO items({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(cols.values()))
            audit(db, g.user, "item", cur.lastrowid, "create", cols)
    attempt(go, "Item saved")
    return redirect(url_for("prod.items"))


@bp.route("/prod/bom/<int:item_id>")
@roles_required(*FLOOR)
def bom(item_id):
    db = get_db()
    product = db.execute("SELECT * FROM items WHERE id=? AND kind='finished'", (item_id,)).fetchone()
    if not product:
        abort(404)
    b = db.execute("SELECT * FROM boms WHERE item_id=?", (item_id,)).fetchone()
    lines = db.execute("""SELECT bl.*, i.name, i.disp_unit, i.factor, i.kind FROM bom_lines bl JOIN items i ON i.id=bl.item_id
                          WHERE bl.bom_id=? ORDER BY i.kind DESC, i.name""", (b["id"],)).fetchall() if b else []
    mats = db.execute("SELECT * FROM items WHERE kind IN ('raw','packaging') AND active=1 ORDER BY kind, name").fetchall()
    return render_template("bom.html", product=product, bom=b, lines=lines, materials=mats, units={i["id"]: units_for(i) for i in mats})


@bp.route("/prod/bom/<int:item_id>/save", methods=["POST"])
@roles_required(*FLOOR)
def bom_save(item_id):
    f = request.form

    def go():
        db = get_db()
        with tx(db):
            b = db.execute("SELECT * FROM boms WHERE item_id=?", (item_id,)).fetchone()
            y = int(f["yield"])
            if y <= 0:
                raise BusinessError("Yield must be positive")
            if b:
                db.execute("UPDATE boms SET yield_packets=? WHERE id=?", (y, b["id"]))
                bid = b["id"]
            else:
                bid = db.execute("INSERT INTO boms(item_id,yield_packets) VALUES (?,?)", (item_id, y)).lastrowid
            if f.get("line_item"):
                item = db.execute("SELECT * FROM items WHERE id=?", (int(f["line_item"]),)).fetchone()
                qty = to_base(f["qty"], f.get("unit"), item)
                if qty <= 0:
                    raise BusinessError("Quantity must be positive")
                db.execute("INSERT OR REPLACE INTO bom_lines(bom_id,item_id,qty_base,per_packet) VALUES (?,?,?,?)",
                           (bid, item["id"], qty, 1 if f.get("per_packet") else 0))
            audit(db, g.user, "bom", bid, "save", dict(f))
    attempt(go, "BOM saved (existing runs keep their original plan)")
    return redirect(url_for("prod.bom", item_id=item_id))


@bp.route("/prod/bom/<int:item_id>/remove/<int:line_id>", methods=["POST"])
@roles_required(*FLOOR)
def bom_remove(item_id, line_id):
    def go():
        db = get_db()
        db.execute("DELETE FROM bom_lines WHERE id=?", (line_id,))
        audit(db, g.user, "bom", item_id, "remove_line", {"line_id": line_id})
    attempt(go, "Line removed")
    return redirect(url_for("prod.bom", item_id=item_id))


# ------------------------------------------------------------ stock adjustment
@bp.route("/prod/adjust", methods=["POST"])
@roles_required(*FLOOR)
def adjust():
    def go():
        db = get_db()
        item = db.execute("SELECT * FROM items WHERE id=?", (int(request.form["item_id"]),)).fetchone()
        sign = -1 if request.form.get("direction") == "out" else 1
        with tx(db):
            qty = sign * to_base(request.form["qty"], item["disp_unit"], item)
            stock.adjust(db, item["id"], qty, g.user, request.form.get("reason", "").strip())
            audit(db, g.user, "stock", item["id"], "adjust", {"qty_base": qty, "reason": request.form.get("reason")})
    attempt(go, "Stock adjustment posted")
    return redirect(url_for("prod.items"))
