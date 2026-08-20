"""SQLite storage.

Three tables carry the product:

  purchases  one row per receipt line item you bought
  deals      one row per coupon-book offer
  alerts     what we've already pushed, so a month-long sale notifies once

A claimable price adjustment is just: a deal whose item_number matches a
purchase still inside Costco's 30-day window, at a lower price than you paid.
The 7-digit item number is printed on both the receipt and the coupon, so the
join is exact — no fuzzy name matching, no false positives.
"""

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    id            INTEGER PRIMARY KEY,
    item_number   TEXT    NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    price_paid    REAL    NOT NULL,
    quantity      INTEGER NOT NULL DEFAULT 1,
    purchased_on  TEXT    NOT NULL,
    warehouse     TEXT    NOT NULL DEFAULT '',
    receipt       TEXT    NOT NULL DEFAULT '',
    UNIQUE(item_number, price_paid, purchased_on)
);

CREATE TABLE IF NOT EXISTS deals (
    id           INTEGER PRIMARY KEY,
    item_number  TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    discount     REAL NOT NULL DEFAULT 0,
    sale_price   REAL NOT NULL DEFAULT 0,
    regular_price REAL NOT NULL DEFAULT 0,
    valid_from   TEXT,
    valid_to     TEXT,
    source       TEXT NOT NULL DEFAULT '',
    imported_on  TEXT NOT NULL,
    UNIQUE(item_number, discount, sale_price, valid_from)
);

-- `kind` distinguishes a "claim now" alert from a "will be claimable" one for
-- the same purchase at the same price. Without it, telling you a sale is
-- coming would suppress the alert that it has actually started -- the one that
-- gets you your money.
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY,
    purchase_id INTEGER NOT NULL,
    price       REAL    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'claim',
    sent_on     TEXT    NOT NULL,
    UNIQUE(purchase_id, price, kind)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_purchases_item ON purchases(item_number);
CREATE INDEX IF NOT EXISTS idx_deals_item     ON deals(item_number);
"""


def _migrate(conn) -> None:
    """Bring an existing database up to the current schema.

    CREATE TABLE IF NOT EXISTS silently leaves an older table alone, so schema
    changes need an explicit step. The live database is committed to the repo
    and is the only copy of your purchase history -- rebuild in place rather
    than dropping.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
    if cols and "kind" not in cols:
        conn.executescript("""
            ALTER TABLE alerts RENAME TO alerts_old;
            CREATE TABLE alerts (
                id          INTEGER PRIMARY KEY,
                purchase_id INTEGER NOT NULL,
                price       REAL    NOT NULL,
                kind        TEXT    NOT NULL DEFAULT 'claim',
                sent_on     TEXT    NOT NULL,
                UNIQUE(purchase_id, price, kind)
            );
            INSERT INTO alerts (purchase_id, price, kind, sent_on)
                SELECT purchase_id, price, 'claim', sent_on FROM alerts_old;
            DROP TABLE alerts_old;
        """)


@contextmanager
def connect():
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_purchase(conn, *, item_number, description, price_paid, quantity,
                 purchased_on, warehouse="", receipt="") -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO purchases
           (item_number, description, price_paid, quantity, purchased_on, warehouse, receipt)
           VALUES (?,?,?,?,?,?,?)""",
        (str(item_number).strip(), description.strip(), float(price_paid),
         int(quantity), purchased_on, warehouse, receipt),
    )
    return cur.rowcount > 0


def add_deal(conn, *, item_number, description, discount, sale_price,
             regular_price=0, valid_from=None, valid_to=None, source="") -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO deals
           (item_number, description, discount, sale_price, regular_price,
            valid_from, valid_to, source, imported_on)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (str(item_number).strip(), description.strip(), float(discount),
         float(sale_price), float(regular_price), valid_from, valid_to, source,
         date.today().isoformat()),
    )
    return cur.rowcount > 0


def open_purchases(conn, window_days=None):
    """Purchases still inside the price-adjustment window."""
    window_days = window_days or config.ADJUSTMENT_WINDOW_DAYS
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    return conn.execute(
        "SELECT * FROM purchases WHERE purchased_on >= ? ORDER BY purchased_on DESC",
        (cutoff,),
    ).fetchall()


def deals_for(conn, item_number):
    return conn.execute(
        "SELECT * FROM deals WHERE item_number = ?", (str(item_number),)
    ).fetchall()


def upcoming_deals(conn):
    """Deals whose sale period hasn't ended — the 'wait, don't buy yet' list."""
    today = date.today().isoformat()
    return conn.execute(
        "SELECT * FROM deals WHERE valid_to IS NULL OR valid_to >= ? "
        "ORDER BY valid_from, item_number",
        (today,),
    ).fetchall()


def already_alerted(conn, purchase_id, price, kind="claim") -> bool:
    # price <= : a deeper discount is worth telling you about again, the same
    # one is not.
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE purchase_id = ? AND price <= ? AND kind = ?",
        (purchase_id, float(price), kind),
    ).fetchone()
    return row is not None


def record_alert(conn, purchase_id, price, kind="claim") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO alerts (purchase_id, price, kind, sent_on) "
        "VALUES (?,?,?,?)",
        (purchase_id, float(price), kind, date.today().isoformat()),
    )


def set_meta(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def touch_checkin(conn) -> None:
    set_meta(conn, "last_run", datetime.now().isoformat(timespec="seconds"))
