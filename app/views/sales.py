"""Salesman mobile screens, admin order desk, invoices, payments, customers, prices."""
from flask import (Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for)

from .. import exporters
from ..money import rupees_to_paise, units_for
from ..services import sales as svc
from ..services.common import BusinessError, setting, today
from .helpers import attempt, get_db, money_input, roles_required

bp = Blueprint("sales", __name__)
INVOICE_VIEW = ("admin", "accountant", "owner")


# ------------------------------------------------------------ salesman (mobile)
@bp.route("/m")
@roles_required("salesman")
def m_home():
    db = get_db()
    orders = db.execute(
        """SELECT o.*, c.name cname, COALESCE((SELECT SUM(qty*price_paise) FROM order_lines WHERE order_id=o.id),0) value
           FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.salesman_id=? ORDER BY o.id DESC LIMIT 50""",
        (g.user["id"],)).fetchall()
    return render_template("m_home.html", orders=orders, by_customer=svc.customer_wise_sales(db, g.user["id"]))


@bp.route("/m/new")
@roles_required("salesman")
def m_new():
    return render_template("m_new.html")


@bp.route("/m/api/catalog")
@roles_required("salesman")
def m_catalog():
    db = get_db()
    customers = db.execute("SELECT id, name, type, state_code FROM customers WHERE active=1 AND (salesman_id=? OR salesman_id IS NULL) ORDER BY name",
                           (g.user["id"],)).fetchall()
    items = db.execute("SELECT * FROM items WHERE kind='finished' AND active=1 ORDER BY name").fetchall()
    prices = {c["id"]: {i["id"]: svc.price_for(db, c["id"], i["id"]) for i in items} for c in customers}
    return jsonify(customers=[dict(c) for c in customers],
                   products=[{"id": i["id"], "name": i["name"], "carton_size": i["carton_size"],
                              "units": units_for(i)} for i in items],
                   prices=prices)


@bp.route("/m/api/orders", methods=["POST"])
@roles_required("salesman")
def m_api_orders():
    """Idempotent on client_uuid so an offline queue can safely retry."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        oid, created = svc.create_order(db, int(data.get("customer_id") or 0), g.user, data.get("lines") or [],
                                        client_uuid=data.get("client_uuid"), notes=data.get("notes"))
    except (BusinessError, ValueError) as e:
        return jsonify(ok=False, error=str(e)), 422
    order = db.execute("SELECT order_no, status FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(ok=True, id=oid, order_no=order["order_no"], status=order["status"], duplicate=not created)


@bp.route("/m/order/<int:oid>")
@roles_required("salesman")
def m_order(oid):
    db = get_db()
    order = db.execute("SELECT o.*, c.name cname FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.id=? AND o.salesman_id=?",
                       (oid, g.user["id"])).fetchone()
    if not order:
        abort(404)
    inv = db.execute("SELECT id, invoice_no, status, dispatched_at FROM invoices WHERE order_id=? ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
    return render_template("m_order.html", order=order, lines=svc.order_lines(db, oid), value=svc.order_value(db, oid), inv=inv)


# ------------------------------------------------------------ admin: orders
@bp.route("/admin/orders")
@roles_required("admin")
def admin_orders():
    status = request.args.get("status", "")
    db = get_db()
    sql = """SELECT o.*, c.name cname, u.full_name sname, COALESCE((SELECT SUM(qty*price_paise) FROM order_lines WHERE order_id=o.id),0) value
             FROM orders o JOIN customers c ON c.id=o.customer_id JOIN users u ON u.id=o.salesman_id"""
    args = []
    if status:
        sql += " WHERE o.status=?"
        args.append(status)
    rows = db.execute(sql + " ORDER BY o.id DESC LIMIT 200", args).fetchall()
    return render_template("admin_orders.html", orders=rows, status=status)


@bp.route("/admin/orders/<int:oid>")
@roles_required("admin")
def admin_order(oid):
    db = get_db()
    order = db.execute("SELECT o.*, c.name cname, c.gstin, c.state_code, u.full_name sname FROM orders o JOIN customers c ON c.id=o.customer_id "
                       "JOIN users u ON u.id=o.salesman_id WHERE o.id=?", (oid,)).fetchone()
    if not order:
        abort(404)
    checks = {c["line_id"]: c for c in svc.stock_check(db, oid)}
    tax = svc.compute_tax(db, oid)
    inv = db.execute("SELECT * FROM invoices WHERE order_id=? ORDER BY id DESC", (oid,)).fetchall()
    return render_template("admin_order.html", order=order, lines=svc.order_lines(db, oid), checks=checks, tax=tax, invoices=inv)


@bp.route("/admin/orders/<int:oid>/edit", methods=["POST"])
@roles_required("admin")
def admin_order_edit(oid):
    def go():
        edits = {int(k[4:]): (int(request.form[k]), rupees_to_paise(request.form["price" + k[4:]]))
                 for k in request.form if k.startswith("qty_")}
        svc.update_order_lines(get_db(), oid, edits, g.user)
    attempt(go, "Order updated")
    return redirect(url_for("sales.admin_order", oid=oid))


@bp.route("/admin/orders/<int:oid>/approve", methods=["POST"])
@roles_required("admin")
def admin_order_approve(oid):
    attempt(lambda: svc.approve_order(get_db(), oid, g.user, allow_shortage=bool(request.form.get("override"))),
            "Order approved")
    return redirect(url_for("sales.admin_order", oid=oid))


@bp.route("/admin/orders/<int:oid>/reject", methods=["POST"])
@roles_required("admin")
def admin_order_reject(oid):
    attempt(lambda: svc.reject_order(get_db(), oid, request.form.get("reason", "").strip(), g.user), "Order rejected")
    return redirect(url_for("sales.admin_order", oid=oid))


@bp.route("/admin/orders/<int:oid>/invoice", methods=["POST"])
@roles_required("admin")
def admin_order_invoice(oid):
    inv_id = attempt(lambda: svc.create_invoice(get_db(), oid, g.user), "GST invoice raised")
    return redirect(url_for("sales.invoice", iid=inv_id) if inv_id else url_for("sales.admin_order", oid=oid))


# ------------------------------------------------------------ invoices
@bp.route("/admin/invoices")
@roles_required(*INVOICE_VIEW)
def invoices():
    rows = get_db().execute(
        """SELECT v.*, c.name cname, v.total_paise - COALESCE((SELECT SUM(amount_paise) FROM payments WHERE invoice_id=v.id),0) due
           FROM invoices v JOIN customers c ON c.id=v.customer_id ORDER BY v.id DESC LIMIT 200""").fetchall()
    return render_template("invoices.html", invoices=rows)


@bp.route("/invoice/<int:iid>")
@roles_required(*INVOICE_VIEW)
def invoice(iid):
    db = get_db()
    inv = svc.get_invoice(db, iid)
    pays = db.execute("SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (iid,)).fetchall()
    cn = db.execute("SELECT * FROM credit_notes WHERE invoice_id=?", (iid,)).fetchone()
    share = url_for("sales.public_invoice", token=inv["share_token"], _external=True)
    return render_template("invoice.html", inv=inv, lines=svc.invoice_lines(db, iid), payments=pays, cn=cn,
                           due=svc.outstanding(db, iid), share_url=share, today=today())


def _company(db):
    return {"name": setting(db, "company_name"), "gstin": setting(db, "seller_gstin"),
            "address": setting(db, "seller_address"), "state_code": setting(db, "seller_state_code")}


def _pdf_response(inv_id):
    db = get_db()
    inv = svc.get_invoice(db, inv_id)
    pdf = exporters.invoice_pdf(inv, svc.invoice_lines(db, inv_id), _company(db))
    name = inv["invoice_no"].replace("/", "-")
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f'inline; filename="{name}.pdf"'})


@bp.route("/invoice/<int:iid>/pdf")
@roles_required(*INVOICE_VIEW)
def invoice_pdf(iid):
    return _pdf_response(iid)


@bp.route("/i/<token>.pdf")
def public_invoice(token):
    """Unguessable link for sharing the PDF with the customer (WhatsApp / e-mail)."""
    row = get_db().execute("SELECT id FROM invoices WHERE share_token=?", (token,)).fetchone()
    if not row:
        abort(404)
    return _pdf_response(row["id"])


@bp.route("/invoice/<int:iid>/dispatch", methods=["POST"])
@roles_required("admin")
def invoice_dispatch(iid):
    attempt(lambda: svc.dispatch(get_db(), iid, g.user), "Dispatch recorded; finished-goods stock reduced")
    return redirect(url_for("sales.invoice", iid=iid))


@bp.route("/invoice/<int:iid>/payment", methods=["POST"])
@roles_required("admin")
def invoice_payment(iid):
    attempt(lambda: svc.record_payment(get_db(), iid, money_input("amount"), request.form.get("mode"), g.user,
                                       ref=request.form.get("ref"), pay_date=request.form.get("date") or None),
            "Payment recorded")
    return redirect(url_for("sales.invoice", iid=iid))


@bp.route("/invoice/<int:iid>/cancel", methods=["POST"])
@roles_required("admin")
def invoice_cancel(iid):
    attempt(lambda: svc.cancel_invoice(get_db(), iid, request.form.get("reason", "").strip(), g.user,
                                       goods_returned=bool(request.form.get("returned"))), "Invoice cancelled with credit note")
    return redirect(url_for("sales.invoice", iid=iid))


# ------------------------------------------------------------ customers & prices
@bp.route("/admin/customers")
@roles_required("admin")
def customers():
    db = get_db()
    rows = db.execute("SELECT c.*, u.full_name sname FROM customers c LEFT JOIN users u ON u.id=c.salesman_id ORDER BY c.name").fetchall()
    items = db.execute("SELECT * FROM items WHERE kind='finished' AND active=1").fetchall()
    cp = db.execute("SELECT cp.*, c.name cname, i.name iname FROM customer_prices cp JOIN customers c ON c.id=cp.customer_id "
                    "JOIN items i ON i.id=cp.item_id ORDER BY c.name").fetchall()
    sm = db.execute("SELECT id, full_name FROM users WHERE role='salesman' AND active=1").fetchall()
    return render_template("customers.html", customers=rows, items=items, cprices=cp, salesmen=sm)


@bp.route("/admin/customers/save", methods=["POST"])
@roles_required("admin")
def customer_save():
    f = request.form

    def go():
        from ..services.common import audit
        db = get_db()
        vals = (f["name"].strip(), f["type"], (f.get("gstin") or "").strip().upper() or None, f["state_code"],
                f.get("address"), f.get("phone"), int(f["salesman_id"]) if f.get("salesman_id") else None)
        if not vals[0]:
            raise BusinessError("Name is required")
        if f.get("id"):
            db.execute("UPDATE customers SET name=?,type=?,gstin=?,state_code=?,address=?,phone=?,salesman_id=? WHERE id=?", (*vals, int(f["id"])))
            audit(db, g.user, "customer", int(f["id"]), "update", dict(f))
        else:
            cur = db.execute("INSERT INTO customers(name,type,gstin,state_code,address,phone,salesman_id) VALUES (?,?,?,?,?,?,?)", vals)
            audit(db, g.user, "customer", cur.lastrowid, "create", dict(f))
    attempt(go, "Customer saved")
    return redirect(url_for("sales.customers"))


@bp.route("/admin/prices", methods=["POST"])
@roles_required("admin")
def price_save():
    f = request.form

    def go():
        from ..services.common import audit
        db = get_db()
        price = rupees_to_paise(f["price"])
        if f.get("customer_id"):
            db.execute("INSERT OR REPLACE INTO customer_prices(customer_id,item_id,price_paise) VALUES (?,?,?)",
                       (int(f["customer_id"]), int(f["item_id"]), price))
        else:
            db.execute("UPDATE items SET price_paise=? WHERE id=?", (price, int(f["item_id"])))
        audit(db, g.user, "price", int(f["item_id"]), "set", dict(f))
    attempt(go, "Price saved")
    return redirect(url_for("sales.customers"))
