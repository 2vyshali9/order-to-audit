from decimal import InvalidOperation
from functools import wraps

from flask import abort, current_app, flash, g, redirect, request, url_for

from .. import db as dbm
from ..services.common import BusinessError


def get_db():
    if "db" not in g:
        g.db = dbm.connect(current_app.config["DB_PATH"])
    return g.db


def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                return redirect(url_for("auth.login", next=request.path))
            if g.user["role"] not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def attempt(fn, ok_msg=None):
    """Run a service call; show business-rule errors as flash messages. Returns result or None."""
    try:
        res = fn()
        if ok_msg:
            flash(ok_msg, "ok")
        return True if res is None else res
    except (BusinessError, ValueError, InvalidOperation, KeyError) as e:
        flash(str(e) or "Invalid input", "error")
    return None


def nav_for(role):
    S = {
        "salesman": [("My orders", "sales.m_home"), ("New order", "sales.m_new")],
        "admin": [("Dashboard", "reports.dashboard"), ("Orders", "sales.admin_orders"), ("Invoices", "sales.invoices"),
                  ("Production", "prod.runs"), ("Purchases", "prod.purchases"), ("Items & BOM", "prod.items"),
                  ("Customers", "sales.customers"), ("Reports", "reports.index")],
        "production": [("Production runs", "prod.runs"), ("Purchases", "prod.purchases"), ("Items & BOM", "prod.items"),
                       ("Stock", "reports.index")],
        "accountant": [("Reports", "reports.index"), ("Invoices", "sales.invoices")],
        "owner": [("Dashboard", "reports.dashboard"), ("Reports", "reports.index"), ("Invoices", "sales.invoices")],
    }
    return [(label, url_for(ep)) for label, ep in S[role]]


def money_input(name="amount"):
    from ..money import rupees_to_paise
    return rupees_to_paise(request.form[name])
