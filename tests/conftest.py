"""Shared fixtures. (Reconstructed: the original conftest was lost; written from how the tests use `db`, `actors`, `app`, `client`, `web`.)"""
import re

import pytest

from app import create_app, db as dbm
from app.seed import DEMO_PASSWORD, seed


@pytest.fixture()
def db():
    conn = dbm.connect(":memory:")
    dbm.init_schema(conn)
    seed(conn)
    yield conn
    conn.close()


@pytest.fixture()
def actors(db):
    return {r["username"]: r for r in db.execute("SELECT * FROM users")}


@pytest.fixture()
def app(tmp_path):
    path = str(tmp_path / "test.db")
    conn = dbm.connect(path)
    dbm.init_schema(conn)
    seed(conn)
    conn.close()
    return create_app({"DB_PATH": path, "TESTING": True, "SECRET_KEY": "test"})


@pytest.fixture()
def client(app):
    return app.test_client()


class WebUser:
    """A logged-in test browser that adds the CSRF token to form posts automatically."""

    def __init__(self, app, username):
        self.c = app.test_client()
        page = self.c.get("/login").get_data(as_text=True)
        tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        r = self.c.post("/login", data={"username": username, "password": DEMO_PASSWORD, "_csrf": tok})
        assert r.status_code == 302, f"login failed for {username}"
        self.c.get("/")                      # make sure the new session has a token
        with self.c.session_transaction() as s:
            self.token = s["csrf"]

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, json=None, **kw):
        if json is not None:
            return self.c.post(url, json=json, **kw)
        return self.c.post(url, data={**(data or {}), "_csrf": self.token}, **kw)


@pytest.fixture()
def web(app):
    return lambda username: WebUser(app, username)
