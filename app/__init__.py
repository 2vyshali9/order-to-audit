"""Flask application factory: config, blueprints, per-request user + CSRF check, Jinja filters."""
import hmac
import os
import secrets

from flask import Flask, abort, g, redirect, render_template, send_from_directory, session, url_for

from . import db as dbm
from .money import fmt_dec, fmt_qty, inr, state_label


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
        DB_PATH=os.environ.get("DB_PATH", os.path.join(app.instance_path, "app.db")),
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    if config:
        app.config.update(config)

    from .services.reports import fmt_cell
    from .views import auth, prod, reports, sales
    from .views.helpers import get_db, nav_for
    for m in (auth, sales, prod, reports):
        app.register_blueprint(m.bp)

    app.jinja_env.filters.update(inr=inr, qty=lambda base, item: fmt_qty(base, item), dec=fmt_dec, state=state_label)
    app.jinja_env.globals.update(zip=zip, fmt_cell=fmt_cell, csrf_value=lambda: session.get("csrf", ""))

    @app.before_request
    def load_user_and_check_csrf():
        session.setdefault("csrf", secrets.token_urlsafe(24))
        g.user = None
        uid = session.get("uid")
        if uid:
            g.user = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        # Form posts must carry the session's token. JSON requests (the offline order API) are exempt:
        # a cross-site page cannot send application/json without a CORS preflight, and cookies are SameSite=Lax.
        from flask import request
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.is_json:
            sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
            if not hmac.compare_digest(sent, session["csrf"]):
                abort(400, "Missing or invalid CSRF token")

    @app.context_processor
    def inject():
        u = g.get("user")
        return {"user": u, "nav": nav_for(u["role"]) if u else [], "csrf_token": session.get("csrf", "")}

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.route("/")
    def home():
        u = g.user
        if not u:
            return redirect(url_for("auth.login"))
        ep = {"salesman": "sales.m_home", "admin": "reports.dashboard", "owner": "reports.dashboard",
              "production": "prod.runs", "accountant": "reports.index"}[u["role"]]
        return redirect(url_for(ep))

    @app.route("/sw.js")
    def service_worker():   # served from the root so its scope covers /m/*
        resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def http_error(e):
        return f"<h1>{e.code} {e.name}</h1><p>{e.description}</p><p><a href='/'>Home</a></p>", e.code

    return app
