"""Load the Section 5 master data.  Usage:  python -m app.seed [--reset] [--if-empty]"""
import argparse
import os
import sys
from datetime import date

from werkzeug.security import generate_password_hash

from . import db as dbm
from .services import stock
from .money import rupees_to_paise as rp

DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo123")

USERS = [
    ("salesA", "Salesman A", "salesman"),
    ("salesB", "Salesman B", "salesman"),
    ("admin", "Factory Admin", "admin"),
    ("production", "Production Supervisor", "production"),
    ("auditor", "Accountant / Auditor", "accountant"),
    ("owner", "Owner", "owner"),
]


def seed(db, month_start=None):
    month_start = month_start or date.today().replace(day=1).isoformat()
    with dbm.tx(db):
        for k, v in {
            "company_name": "Sri Murugan Snacks (demo)",
            "seller_gstin": "33ABCDE1234F1Z5",          # dummy, valid-format GSTIN
            "seller_state_code": "33",                    # Tamil Nadu
            "seller_address": "12 Industrial Estate, Tiruchirappalli, Tamil Nadu 620001",
            "variance_tolerance_pct": "3",
        }.items():
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, v))
        pw = generate_password_hash(DEMO_PASSWORD)
        for u, name, role in USERS:
            db.execute("INSERT INTO users(username,full_name,role,password_hash) VALUES (?,?,?,?)", (u, name, role, pw))
        uid = {r["username"]: r["id"] for r in db.execute("SELECT id, username FROM users")}

        def item(code, name, kind, base, disp, factor, **kw):
            cols = dict(code=code, name=name, kind=kind, base_unit=base, disp_unit=disp, factor=factor, **kw)
            cur = db.execute(f"INSERT INTO items({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", list(cols.values()))
            return cur.lastrowid

        flour = item("FLOUR", "Flour", "raw", "g", "kg", 1000, hsn="1101", reorder_level_base=50000)
        oil = item("OIL", "Cooking oil", "raw", "ml", "L", 1000, hsn="1512", reorder_level_base=10000)
        spice = item("SPICE", "Salt and spice mix", "raw", "g", "kg", 1000, hsn="0910", reorder_level_base=2000)
        pouch = item("POUCH200", "200 g pouch", "packaging", "piece", "piece", 1, reorder_level_base=200)
        carton = item("CARTON20", "Carton (holds 20 packets)", "packaging", "piece", "piece", 1, reorder_level_base=10)
        mur = item("MUR200", "Murukku 200 g", "finished", "packet", "packet", 1, hsn="21069099", gst_rate_bp=500,
                   price_paise=rp(50), carton_size=20, shelf_life_days=90)

        bom = db.execute("INSERT INTO boms(item_id, yield_packets, note) VALUES (?,?,?)",
                         (mur, 200, "Standard batch: 200 packets of 200 g")).lastrowid
        for it, qty, per_packet in ((flour, 40_000, 0), (oil, 10_000, 0), (spice, 1_200, 0), (pouch, 200, 1), (carton, 10, 0)):
            db.execute("INSERT INTO bom_lines(bom_id,item_id,qty_base,per_packet) VALUES (?,?,?,?)", (bom, it, qty, per_packet))

        db.execute("INSERT INTO suppliers(name,gstin,state_code,phone) VALUES (?,?,?,?)",
                   ("Sri Lakshmi Flour Traders", "33AAACL1234M1Z2", "33", "9000000001"))
        db.execute("INSERT INTO customers(name,type,gstin,state_code,address,phone,salesman_id) VALUES (?,?,?,?,?,?,?)",
                   ("Retailer A", "retailer", "33AAAPA1234C1ZV", "33", "Thillai Nagar, Tiruchirappalli, Tamil Nadu",
                    "9000000002", uid["salesA"]))
        db.execute("INSERT INTO customers(name,type,gstin,state_code,address,phone,salesman_id) VALUES (?,?,?,?,?,?,?)",
                   ("Distributor B", "distributor", "29AAACB1234D1Z7", "29", "Gandhi Bazaar, Bengaluru, Karnataka",
                    "9000000003", uid["salesB"]))

        # Opening stock as ledger rows (value = qty x opening rate). Finished-goods opening value is an
        # assumption (Rs 30/packet): the brief gives none. It affects valuation/COGS only, not cost per packet.
        opening = [(flour, 100_000, 100 * rp(42)), (oil, 30_000, 30 * rp(140)), (spice, 5_000, 5 * rp(200)),
                   (pouch, 500, 500 * rp("2.50")), (carton, 30, 30 * rp(15)), (mur, 50, 50 * rp(30))]
        for item_id, qty, value in opening:
            stock.receive(db, item_id, qty, value, mtype="OPENING", ref_type="opening", ref_no="OPENING",
                          on_date=month_start, note="Opening balance")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="delete the database first")
    ap.add_argument("--if-empty", action="store_true", help="seed only when the database has no users")
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "instance/app.db"))
    a = ap.parse_args()
    if a.reset and os.path.exists(a.db):
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(a.db + ext):
                os.remove(a.db + ext)
    conn = dbm.connect(a.db)
    if dbm.is_empty(conn):
        dbm.init_schema(conn) if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='users'").fetchone() else None
        seed(conn)
        print(f"Seeded {a.db}. Logins (password '{DEMO_PASSWORD}'): " + ", ".join(u[0] for u in USERS))
    elif a.if_empty:
        print("Database already seeded; nothing to do.")
    else:
        sys.exit("Database already has data. Use --reset to start over.")


if __name__ == "__main__":
    main()
