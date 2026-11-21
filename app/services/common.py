import json
from datetime import date, datetime


class BusinessError(Exception):
    """A rule of the business was violated; the message is safe to show the user."""


class InsufficientStock(BusinessError):
    def __init__(self, item_name, needed, available, unit=""):
        self.item_name, self.needed, self.available = item_name, needed, available
        super().__init__(f"Insufficient stock of {item_name}: need {needed}, have {available} {unit}".strip())


class ShortageError(BusinessError):
    def __init__(self, shortages):
        self.shortages = shortages  # list of dicts: item, ordered, available, short, batches_needed
        msg = "; ".join(f"{s['name']}: ordered {s['ordered']}, available {s['available']}, short {s['short']}"
                        for s in shortages)
        super().__init__("Stock shortage - " + msg)


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return date.today().isoformat()


def setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def next_no(db, name: str) -> int:
    db.execute("INSERT OR IGNORE INTO counters(name, value) VALUES (?, 0)", (name,))
    db.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
    return db.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()["value"]


def uid(user):
    return user["id"] if user else None


def audit(db, user, entity: str, entity_id, action: str, details=None):
    db.execute(
        "INSERT INTO audit_log(ts, user_id, username, entity, entity_id, action, details) VALUES (?,?,?,?,?,?,?)",
        (now_ts(), uid(user), user["username"] if user else "system", entity, entity_id, action,
         json.dumps(details, default=str, sort_keys=True) if details is not None else None),
    )


def get_item(db, item_id):
    row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise BusinessError(f"Unknown item {item_id}")
    return row
