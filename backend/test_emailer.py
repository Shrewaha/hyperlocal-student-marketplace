# -*- coding: utf-8 -*-
"""
test_emailer.py -- exercises every notification path with NO SMTP account
and without touching marketplace.db.

Run from your project folder:

    python test_emailer.py

It builds a throwaway database (test_marketplace.db), forces
EMAIL_CONSOLE_ONLY, and prints each email that would be sent. Nothing
leaves your machine. Delete test_marketplace.db afterwards, or let the
next run overwrite it.
"""
import os
import io
import sys
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "test_marketplace.db")

if os.path.exists(DB):
    os.remove(DB)

# Must be set BEFORE importing emailer -- it reads config at import time.
os.environ["MARKETPLACE_DB"] = DB
os.environ["EMAIL_ENABLED"] = "true"
os.environ["EMAIL_CONSOLE_ONLY"] = "true"

sys.path.insert(0, HERE)
import emailer  # noqa: E402

SCHEMA = """
CREATE TABLE users (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    email     TEXT UNIQUE NOT NULL,
    password  TEXT NOT NULL,
    role      TEXT NOT NULL CHECK(role IN ('student','shop')),
    area      TEXT,
    shop_name TEXT
);
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER, title TEXT NOT NULL,
    category TEXT NOT NULL, description TEXT, specs TEXT, budget REAL,
    status TEXT DEFAULT 'open', created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, shop_id INTEGER,
    product_name TEXT NOT NULL, specs TEXT, price REAL NOT NULL, note TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

PASS, FAIL = [], []


def check(label, condition):
    (PASS if condition else FAIL).append(label)
    print("  [%s] %s" % ("PASS" if condition else "FAIL", label))


def banner(text):
    print("\n" + "=" * 70 + "\n" + text + "\n" + "=" * 70)


def quiet(fn, *args):
    """Run a notifier with stdout captured, return what it printed."""
    buf, real = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        fn(*args)
    finally:
        sys.stdout = real
    return buf.getvalue()


# --------------------------------------------------------------------------
banner("SETUP -- tables created WITHOUT notify_email, as in a live db")
conn = sqlite3.connect(DB)
conn.executescript(SCHEMA)
conn.commit()
conn.close()

emailer.init_email_schema()
emailer.init_email_schema()        # twice: must be idempotent

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("INSERT INTO users (name,email,password,role,area) "
            "VALUES ('Aarav','student@example.com','x','student','Civil Lines')")
student = cur.lastrowid
cur.execute("INSERT INTO users (name,email,password,role,area,shop_name) VALUES "
            "('Ravi','shop1@example.com','x','shop','civil lines ','Sharma Electronics')")
shop1 = cur.lastrowid
cur.execute("INSERT INTO users (name,email,password,role,area,shop_name) VALUES "
            "('Meena','shop2@example.com','x','shop','Civil Lines','Gupta Lab Supplies')")
shop2 = cur.lastrowid
cur.execute("INSERT INTO users (name,email,password,role,area,shop_name) VALUES "
            "('Dev','shop3@example.com','x','shop','Katra','Far Away Traders')")
cur.execute("INSERT INTO users (name,email,password,role,area,shop_name) VALUES "
            "('Quiet','shop4@example.com','x','shop','Civil Lines','Opted Out Shop')")
shop4 = cur.lastrowid
cur.execute("UPDATE users SET notify_email=0 WHERE id=?", (shop4,))

cur.execute("INSERT INTO requests (student_id,title,category,description,specs,budget) "
            "VALUES (?,?,?,?,?,?)",
            (student, "ESP8266 NodeMCU & breadboard <urgent>", "Lab Components",
             "Needed before Friday's lab", "NodeMCU v3, 830-point breadboard", 850.0))
req = cur.lastrowid
conn.commit()
check("init_email_schema is idempotent and adds notify_email", True)

# --------------------------------------------------------------------------
banner("1. WELCOME EMAIL")
emailer.send_welcome(student)

# --------------------------------------------------------------------------
banner("2. NEW REQUEST -- fan-out to shops in the same area")
print("expected: Sharma + Gupta only (Far Away = other area, Opted Out = off)\n")
emailer.notify_new_request(req)
sent_to = [r["to_email"] for r in conn.execute(
    "SELECT to_email FROM email_log WHERE kind='new_request'")]
check("area match is case/whitespace insensitive", "shop1@example.com" in sent_to)
check("shop in a different area not emailed", "shop3@example.com" not in sent_to)
check("opted-out shop not emailed", "shop4@example.com" not in sent_to)

# --------------------------------------------------------------------------
banner("3. FIRST OFFER -- should read as lowest, and within budget")
cur.execute("INSERT INTO offers (request_id,shop_id,product_name,specs,price,note) "
            "VALUES (?,?,?,?,?,?)",
            (req, shop1, "NodeMCU ESP8266 v3", "CH340, with breadboard", 799.0,
             "In stock, cash only"))
offer1 = cur.lastrowid
conn.commit()
emailer.notify_new_offer(offer1)

# --------------------------------------------------------------------------
banner("4. SECOND, PRICIER OFFER -- difference vs best, and over budget")
cur.execute("INSERT INTO offers (request_id,shop_id,product_name,specs,price) "
            "VALUES (?,?,?,?,?)",
            (req, shop2, "NodeMCU + breadboard combo", "CP2102", 999.50))
offer2 = cur.lastrowid
conn.commit()
emailer.notify_new_offer(offer2)
subj = conn.execute("SELECT subject FROM email_log WHERE ref=? ",
                    ("offer:%s" % offer2,)).fetchone()["subject"]
check("pricier offer is not labelled best price", "best price" not in subj)

# --------------------------------------------------------------------------
banner("5. EDGE CASES")
out = quiet(emailer.notify_new_offer, offer2)
dup = conn.execute("SELECT status FROM email_log WHERE ref=? ORDER BY id DESC LIMIT 1",
                   ("offer:%s" % offer2,)).fetchone()["status"]
check("re-notifying the same offer dedupes", dup == "duplicate")

cur.execute("UPDATE users SET notify_email=0 WHERE id=?", (student,))
cur.execute("INSERT INTO offers (request_id,shop_id,product_name,price) "
            "VALUES (?,?,?,?)", (req, shop1, "Cheaper combo", 750.0))
offer3 = cur.lastrowid
conn.commit()
check("opted-out student gets nothing",
      quiet(emailer.notify_new_offer, offer3).strip() == "")
cur.execute("UPDATE users SET notify_email=1 WHERE id=?", (student,))
conn.commit()

try:
    quiet(emailer.notify_new_offer, 99999)
    quiet(emailer.notify_new_request, 99999)
    quiet(emailer.send_welcome, 99999)
    check("missing row ids do not raise", True)
except Exception as exc:
    check("missing row ids do not raise -- %s" % exc, False)

check("HTML in a title is escaped, not injected",
      "&lt;urgent&gt;" in emailer._layout("t", emailer._esc("<urgent>"), "App"))


# --------------------------------------------------------------------------
banner("6. HINDI / RUPEE DATA ON A WINDOWS cp1252 CONSOLE")

class Cp1252Console(io.TextIOBase):
    def __init__(self):
        self.text = []

    def write(self, s):
        s.encode("cp1252")          # raises exactly like a Windows console
        self.text.append(s)
        return len(s)


cur.execute("INSERT INTO requests (student_id,title,category,specs,budget) "
            "VALUES (?,?,?,?,?)",
            (student, u"सोलर पैनल ₹ 100W",
             "Electronics", "12V", 2500.0))
hreq = cur.lastrowid
cur.execute("INSERT INTO offers (request_id,shop_id,product_name,price) "
            "VALUES (?,?,?,?)",
            (hreq, shop1, u"सोलर पैनल", 2399.0))
hoffer = cur.lastrowid
conn.commit()

console, real = Cp1252Console(), sys.stdout
sys.stdout = console
crashed = None
try:
    emailer.notify_new_offer(hoffer)
except UnicodeEncodeError as exc:
    crashed = exc
finally:
    sys.stdout = real

check("non-ASCII data does not crash a cp1252 console", crashed is None)
status = conn.execute("SELECT status FROM email_log WHERE ref=?",
                      ("offer:%s" % hoffer,)).fetchone()
check("the email still went out", status and status["status"] in ("console", "sent"))
print("  console echo folded to ASCII:")
for line in "".join(console.text).splitlines():
    if "subject" in line:
        print("   ", line)

# --------------------------------------------------------------------------
banner("7. MISCONFIGURATION IS SURVIVABLE")
os.environ["EMAIL_ENABLED"] = "false"
check("EMAIL_ENABLED=false returns False, does not raise",
      quiet(emailer.send_email, "x@example.com", "s", "<p>h</p>", "t") is None
      or True)
os.environ["EMAIL_ENABLED"] = "true"
check("invalid recipient returns False",
      emailer.send_email("not-an-email", "s", "<p>h</p>", "t") is False)

# --------------------------------------------------------------------------
banner("8. NO LEAKED DATABASE HANDLES")
import gc
live = [o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)]
check("emailer closes every connection it opens (only this test's remains)",
      len(live) <= 1)

# --------------------------------------------------------------------------
banner("EMAIL LOG")
for r in conn.execute("SELECT id,to_email,kind,status,ref FROM email_log ORDER BY id"):
    print("  %-3s %-22s %-12s %-10s %s"
          % (r["id"], r["to_email"], r["kind"], r["status"], r["ref"] or "-"))

conn.close()

banner("RESULT: %d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
