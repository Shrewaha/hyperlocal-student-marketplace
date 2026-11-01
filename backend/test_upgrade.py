# -*- coding: utf-8 -*-
"""
test_upgrade.py -- an existing database must survive the upgrade.

The release that added session expiry, the age gate and password resets also
added columns and a table. Anyone already running this has a marketplace.db
built by the previous version, and starting the new code against it must not
drop their accounts or sign everybody out.

This builds a database shaped the way the previous release left it, points
main.py at it, and checks what the migration did.

Run:
    cd backend && python test_upgrade.py
"""

import os
import sys
import sqlite3
import tempfile

DB = os.path.join(tempfile.mkdtemp(), "old.db")

# The previous release's shape: no sessions.expires_at, no
# users.adult_confirmed_at, no password_resets table.
_old = sqlite3.connect(DB)
_old.executescript(
    """
    CREATE TABLE users (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        email      TEXT UNIQUE NOT NULL,
        password   TEXT NOT NULL,
        role       TEXT NOT NULL CHECK(role IN ('student','shop')),
        area       TEXT,
        shop_name  TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE sessions (
        token      TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """
)
_old.execute("INSERT INTO users (name, email, password, role) VALUES (?,?,?,?)",
             ("Already Here", "old@example.com", "x", "student"))
_old.execute("INSERT INTO sessions (token, user_id) VALUES (?,?)",
             ("a-token-from-before", 1))
_old.commit()
_old.close()

os.environ["MARKETPLACE_DB"] = DB
os.environ["ADMIN_EMAILS"] = ""
os.environ["EMAIL_CONSOLE_ONLY"] = "true"
os.environ["SESSION_TTL_DAYS"] = "30"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import email_validator                      # noqa: F401
except ImportError:
    import pydantic
    pydantic.EmailStr = str

import main                                     # noqa: E402

FAIL = []


def check(label, got, want=True):
    if got == want:
        print("  [PASS] %s" % label)
    else:
        FAIL.append(label)
        print("  [FAIL] %s  (got %r, wanted %r)" % (label, got, want))


def columns(table):
    return [r["name"] for r in main.connect().execute(
        "PRAGMA table_info(%s)" % table)]


print("\nUPGRADING A DATABASE FROM THE PREVIOUS RELEASE")
print("=" * 62)

check("users gained age", "age" in columns("users"))
check("users gained adult_confirmed_at", "adult_confirmed_at" in columns("users"))
check("sessions gained expires_at", "expires_at" in columns("sessions"))

# Accounts made before the age field have no age, and nothing invents one for
# them. A default of 0 would read as a newborn; blank reads as not asked.
check("an account from before the age field has no age, not a zero",
      main.connect().execute(
          "SELECT age FROM users WHERE email='old@example.com'"
      ).fetchone()["age"], None)
check("and the owner listing does not call them a minor",
      main.admin_users(None, None, main.connect(),
                       {"email": "x@example.com", "id": 0})["users"][0]["minor"],
      False)
check("password_resets was created",
      main.connect().execute(
          "SELECT COUNT(*) c FROM password_resets").fetchone()["c"], 0)

row = main.connect().execute(
    "SELECT expires_at FROM sessions WHERE token='a-token-from-before'"
).fetchone()
check("the session that was already there got an expiry",
      row["expires_at"] is not None)

days = (main.parse_stamp(row["expires_at"]) - main.utcnow()).days
check("and it is a fresh window, not an expired one", 28 <= days <= 30, True)

user = main.get_current_user("Bearer a-token-from-before", main.connect())
check("so nobody is signed out by the upgrade", user["email"], "old@example.com")

check("the account itself is untouched",
      main.connect().execute(
          "SELECT COUNT(*) c FROM users").fetchone()["c"], 1)

# Render restarts the service more than once, and init_db runs every boot.
main.init_db()
main.init_db()
check("init_db is idempotent", True)
check("repeat runs did not duplicate the session",
      main.connect().execute(
          "SELECT COUNT(*) c FROM sessions").fetchone()["c"], 1)

print("=" * 62)
print("RESULT: %s" % ("all checks passed" if not FAIL
                      else "%d FAILED: %s" % (len(FAIL), ", ".join(FAIL))))
sys.exit(1 if FAIL else 0)
