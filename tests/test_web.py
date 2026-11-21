import io
import re

import pytest
from openpyxl import load_workbook

from app import db as dbm


def q(app, sql, *a):
    c = dbm.connect(app.config["DB_PATH"])
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


def test_login_required_and_bad_password(app, client):
    assert client.get("/admin/orders").status_code == 302
    page = client.get("/login").get_data(as_text=True)
    tok = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    r = client.post("/login", data={"username": "admin", "password": "nope", "_csrf": tok})
    assert r.status_code == 200 and b"Wrong username" in r.data


def test_csrf_blocks_forged_form_post(web):
    admin = web("admin")
    r = admin.c.post("/admin/orders/1/approve", data={})
    assert r.status_code == 400


@pytest.mark.parametrize("user,allowed,denied", [
    ("salesA", ["/m", "/m/new"], ["/admin/orders", "/prod", "/reports", "/admin/invoices", "/dashboard"]),
    ("production", ["/prod", "/prod/items", "/prod/purchases", "/reports/stock_summary", "/reports/production_report"],
     ["/admin/orders", "/admin/customers", "/reports/sales_register", "/reports/gst_summary", "/admin/invoices", "/m"]),
    ("auditor", ["/reports", "/reports/sales_register", "/reports/gst_summary", "/admin/invoices"], ["/admin/orders", "/prod", "/m", "/prod/items"]),
    ("owner", ["/dashboard", "/reports", "/admin/invoices"], ["/admin/orders", "/prod", "/m"]),
    ("admin", ["/admin/orders", "/prod", "/dashboard", "/reports/audit_report", "/admin/customers"], ["/m"]),
])
def test_role_based_access(web, user, allowed, denied):
    w = web(user)
    for url in allowed:
        assert w.get(url).status_code == 200, (user, url)
    for url in denied:
        assert w.get(url).status_code in (302, 403), (user, url)


def test_auditor_and_owner_are_read_only(web, app):
    for user in ("auditor", "owner"):
        w = web(user)
        assert w.post("/invoice/1/dispatch").status_code == 403
        assert w.post("/prod/runs/start", data={"item_id": 6}).status_code == 403
        assert w.post("/admin/orders/1/approve").status_code == 403


def test_salesman_sees_only_own_orders(web, app):
    a, b = web("salesA"), web("salesB")
    cat = a.get("/m/api/catalog").get_json()
    assert [c["name"] for c in cat["customers"]] == ["Retailer A"]
    r = a.post("/m/api/orders", json={"client_uuid": "u1", "customer_id": cat["customers"][0]["id"],
                                      "lines": [{"item_id": cat["products"][0]["id"], "qty": 3, "unit": "packet"}]})
    oid = r.get_json()["id"]
    assert a.get(f"/m/order/{oid}").status_code == 200
    assert b.get(f"/m/order/{oid}").status_code == 404


def test_offline_api_is_idempotent_and_validates(web):
    a = web("salesA")
    cat = a.get("/m/api/catalog").get_json()
    body = {"client_uuid": "dup-1", "customer_id": cat["customers"][0]["id"], "lines": [{"item_id": cat["products"][0]["id"], "qty": 2, "unit": "carton"}]}
    r1, r2 = a.post("/m/api/orders", json=body).get_json(), a.post("/m/api/orders", json=body).get_json()
    assert r1["ok"] and r2["ok"] and r2["duplicate"] and r1["id"] == r2["id"]
    bad = a.post("/m/api/orders", json={**body, "client_uuid": "x", "lines": []})
    assert bad.status_code == 422 and not bad.get_json()["ok"]


def test_full_benchmark_through_the_web_app(web, app):
    prod, adm = web("production"), web("admin")
    sa, sb = web("salesA"), web("salesB")
    item = {r["code"]: r["id"] for r in q(app, "SELECT id, code FROM items")}
    cust = {r["name"]: r["id"] for r in q(app, "SELECT id, name FROM customers")}
    sup = q(app, "SELECT id FROM suppliers")[0]["id"]

    # 1 purchase 60 kg flour @ 45
    r = prod.post("/prod/purchases/new", {"supplier_id": sup, "item1": item["FLOUR"], "qty1": "60", "unit1": "kg", "rate1": "45", "gst1": "0"})
    assert r.status_code == 302
    # 2 start run, issue 40 kg / 10.5 L / 1.2 kg
    r = prod.post("/prod/runs/start", {"item_id": item["MUR200"], "batches": "1"})
    run_url = r.headers["Location"]
    rid = int(run_url.rsplit("/", 1)[1])
    for code, qty, unit in (("FLOUR", "40", "kg"), ("OIL", "10.5", "L"), ("SPICE", "1.2", "kg")):
        assert prod.post(f"/prod/runs/{rid}/issue", {"item_id": item[code], "qty": qty, "unit": unit}).status_code == 302
    # 3 actual output
    prod.post(f"/prod/runs/{rid}/complete", {"good": "188", "damaged": "6", f"pack_{item['CARTON20']}": "9"})
    page = prod.get(run_url).get_data(as_text=True)
    assert "-6.00%" in page and "FLAGGED" in page and "+5.00%" in page
    lv = {r["code"]: r["q"] for r in q(app, "SELECT i.code, SUM(l.qty_base) q FROM items i JOIN stock_ledger l ON l.item_id=i.id GROUP BY i.id")}
    assert lv["FLOUR"] == 120000 and lv["OIL"] == 19500 and lv["POUCH200"] == 306 and lv["MUR200"] == 238

    # 4 salesman A orders 150 on mobile
    ca = sa.get("/m/api/catalog").get_json()
    r = sa.post("/m/api/orders", json={"client_uuid": "A1", "customer_id": cust["Retailer A"], "lines": [{"item_id": item["MUR200"], "qty": 150, "unit": "packet"}]}).get_json()
    oa = r["id"]
    # 5 admin approves, invoices, dispatches
    assert "Covered" in adm.get(f"/admin/orders/{oa}").get_data(as_text=True)
    adm.post(f"/admin/orders/{oa}/approve")
    r = adm.post(f"/admin/orders/{oa}/invoice")
    inv_url = r.headers["Location"]
    iid = int(inv_url.rsplit("/", 1)[1])
    adm.post(f"/invoice/{iid}/dispatch")
    page = adm.get(inv_url).get_data(as_text=True)
    assert "7,875.00" in page and "187.50" in page
    pdf = adm.get(f"/invoice/{iid}/pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")
    token = q(app, "SELECT share_token FROM invoices WHERE id=?", iid)[0]["share_token"]
    assert web("auditor").get(f"/i/{token}.pdf").data.startswith(b"%PDF")        # share link
    assert sa.get(f"/m/order/{oa}").get_data(as_text=True).count("dispatched") >= 1

    # 6 salesman B orders 120: shortage flagged, approval blocked, IGST preview
    r = sb.post("/m/api/orders", json={"client_uuid": "B1", "customer_id": cust["Distributor B"], "lines": [{"item_id": item["MUR200"], "qty": 120, "unit": "packet"}]}).get_json()
    ob = r["id"]
    page = adm.get(f"/admin/orders/{ob}").get_data(as_text=True)
    assert "SHORT by 32" in page and "Inter-state: IGST" in page
    adm.post(f"/admin/orders/{ob}/approve")
    assert q(app, "SELECT status FROM orders WHERE id=?", ob)[0]["status"] == "submitted"
    # 7 payment of 4000 from Retailer A
    adm.post(f"/invoice/{iid}/payment", {"amount": "4000", "mode": "upi", "ref": "UPI1"})
    assert "3,875.00" in adm.get(inv_url).get_data(as_text=True)
    # 8 overheads
    prod.post(f"/prod/runs/{rid}/overhead", {"kind": "labour", "amount": "1200"})
    prod.post(f"/prod/runs/{rid}/overhead", {"kind": "gas", "amount": "600"})
    assert "₹31.14" in prod.get(run_url).get_data(as_text=True)

    # 9 reports & exports (auditor, read-only)
    aud = web("auditor")
    for key in ("sales_register", "customer_sales", "product_sales", "gst_summary", "gstr1", "hsn_summary", "purchase_register",
                "stock_summary", "stock_ledger", "production_report", "costing_report", "payments_report", "outstanding_report",
                "outstanding_invoices", "audit_report"):
        assert aud.get(f"/reports/{key}").status_code == 200, key
        assert aud.get(f"/reports/{key}?fmt=csv").status_code == 200, key
        assert aud.get(f"/reports/{key}?fmt=xlsx").data[:2] == b"PK", key
        assert aud.get(f"/reports/{key}?fmt=pdf").data[:4] == b"%PDF", key
    csv_text = aud.get("/reports/stock_summary?fmt=csv").get_data(as_text=True)
    assert "Flour,raw,kg,100,60,40,0,120" in csv_text
    wb = load_workbook(io.BytesIO(aud.get("/reports/sales_register?fmt=xlsx").data))
    assert wb.active["K3"].value == 7875
    gst = aud.get("/reports/gst_summary?fmt=csv").get_data(as_text=True)
    assert "7500.00,0.00,187.50,187.50,375.00" in gst
    assert "3875.00" in aud.get("/reports/outstanding_report?fmt=csv").get_data(as_text=True)
    own = web("owner").get("/dashboard").get_data(as_text=True)
    assert "Variance alerts" in own and "Cooking oil" in own and "31.14" in own


def test_reports_filter_by_date_range(web, app):
    adm = web("admin")
    page = adm.get("/reports/stock_ledger?from=2000-01-01&to=2000-01-31").get_data(as_text=True)
    assert "No data in this period" in page
