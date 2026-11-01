# -*- coding: utf-8 -*-
"""
test_auth.py -- sign-in, sessions, password reset and account deletion.

Calls the endpoint functions in main.py directly against a real SQLite
database, rather than over HTTP. That keeps the test quick and dependency-free
while still running the actual code paths: the same functions uvicorn calls,
the same SQL, the same guards.

Run:
    cd backend && python test_auth.py
"""

import os
import sys
import tempfile

# A throwaway database per run, named before main.py is imported because
# main.py reads MARKETPLACE_DB and builds its schema at import time.
DB = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["MARKETPLACE_DB"] = DB
os.environ["ADMIN_EMAILS"] = "owner@example.com"
os.environ["EMAIL_CONSOLE_ONLY"] = "true"
os.environ["APP_URL"] = "https://example.test/app"
os.environ["SESSION_TTL_DAYS"] = "30"
os.environ["RESET_TTL_MINUTES"] = "60"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import email_validator                      # noqa: F401
except ImportError:
    # EmailStr refuses to build a validator without it. The field type is not
    # what this file tests, so a plain string stands in.
    import pydantic
    pydantic.EmailStr = str

from fastapi import BackgroundTasks, HTTPException   # noqa: E402

import main                                          # noqa: E402

PASS, FAIL = [], []


class FakeRequest(object):
    """Just the two attributes client_ip() reads.

    Not fastapi.Request: building one of those needs an ASGI scope, and the
    code under test only ever looks at headers and client.host.
    """

    class _Client(object):
        def __init__(self, host):
            self.host = host

    def __init__(self, headers=None, host="203.0.113.7"):
        self.headers = dict((k.lower(), v) for k, v in (headers or {}).items())
        self.client = self._Client(host)


def check(label, got, want=True):
    if got == want:
        PASS.append(label)
        print("  ok   %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL %s  (got %r, wanted %r)" % (label, got, want))


def raises(label, status, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except HTTPException as exc:
        check("%s -> %d" % (label, status), exc.status_code, status)
        return exc
    except Exception as exc:
        FAIL.append(label)
        print("  FAIL %s raised %s: %s" % (label, type(exc).__name__, exc))
        return None
    FAIL.append(label)
    print("  FAIL %s did not raise" % label)
    return None


def db():
    return main.connect()


def req(ip="203.0.113.7"):
    return FakeRequest(headers={"x-forwarded-for": ip})


def banner(text):
    print("\n" + text)
    print("-" * len(text))


def reset_limits():
    main._rate_hits.clear()


# ---------------------------------------------------------------- age gate
banner("1. AGE GATE")

bg = BackgroundTasks()
raises("register without is_adult", 400, main.register,
       main.RegisterIn(name="Minor", email="minor@example.com",
                       password="secret123", role="student", area="Ayodhya"),
       bg, req(), db())

bg = BackgroundTasks()
out = main.register(
    main.RegisterIn(name="Asha", email="asha@example.com", password="secret123",
                    role="student", area="Ayodhya", is_adult=True),
    bg, req(), db())
check("register with is_adult succeeds", out["ok"])
STUDENT_ID = out["id"]

row = db().execute("SELECT adult_confirmed_at FROM users WHERE id=?",
                   (STUDENT_ID,)).fetchone()
check("adult_confirmed_at is recorded", row["adult_confirmed_at"] is not None)
check("welcome email was queued", len(bg.tasks), 1)

bg = BackgroundTasks()
out = main.register(
    main.RegisterIn(name="Ravi", email="ravi@example.com", password="secret123",
                    role="shop", shop_name="Ravi Stationers", area="Ayodhya",
                    phone="9876543210", is_adult=True),
    bg, req(), db())
SHOP_ID = out["id"]
check("shop registers", out["ok"])

# ------------------------------------------------------------ token expiry
banner("2. TOKEN EXPIRY")

reset_limits()
raises("login with wrong password", 401, main.login,
       main.LoginIn(email="asha@example.com", password="wrong"), req(), db())

reset_limits()
res = main.login(main.LoginIn(email="asha@example.com", password="secret123"),
                 req(), db())
TOKEN = res["token"]
check("login returns a token", bool(TOKEN))
check("login reports the window", res["expires_in_days"], 30)

row = db().execute("SELECT expires_at FROM sessions WHERE token=?",
                   (TOKEN,)).fetchone()
check("session has an expiry", row["expires_at"] is not None)
days = (main.parse_stamp(row["expires_at"]) - main.utcnow()).days
check("expiry is about 30 days out", 28 <= days <= 30, True)

user = main.get_current_user("Bearer " + TOKEN, db())
check("fresh token authenticates", user["email"], "asha@example.com")
check("session column is not leaked to callers",
      "session_expires_at" in user, False)

# Push the expiry into the past and confirm the token stops working AND the
# row is cleaned up rather than left behind.
conn = db()
conn.execute("UPDATE sessions SET expires_at = ? WHERE token = ?",
             (main.stamp(main.utcnow() - main.timedelta(seconds=1)), TOKEN))
conn.commit()
raises("expired token", 401, main.get_current_user, "Bearer " + TOKEN, db())
left = db().execute("SELECT COUNT(*) c FROM sessions WHERE token=?",
                    (TOKEN,)).fetchone()["c"]
check("expired session row is deleted", left, 0)

# Sliding window: a session near the end of its life is pushed back out.
reset_limits()
TOKEN = main.login(main.LoginIn(email="asha@example.com", password="secret123"),
                   req(), db())["token"]
conn = db()
conn.execute("UPDATE sessions SET expires_at = ? WHERE token = ?",
             (main.stamp(main.utcnow() + main.timedelta(days=3)), TOKEN))
conn.commit()
main.get_current_user("Bearer " + TOKEN, db())
row = db().execute("SELECT expires_at FROM sessions WHERE token=?",
                   (TOKEN,)).fetchone()
days = (main.parse_stamp(row["expires_at"]) - main.utcnow()).days
check("use slides the window back to ~30 days", 28 <= days <= 30, True)

# Logging out everywhere kills this token and every other one.
reset_limits()
second = main.login(main.LoginIn(email="asha@example.com",
                                 password="secret123"), req(), db())["token"]
out = main.logout_everywhere(db(), main.get_current_user("Bearer " + TOKEN, db()))
check("logout-everywhere reports a count", out["signed_out"] >= 2, True)
raises("first token after logout-everywhere", 401,
       main.get_current_user, "Bearer " + TOKEN, db())
raises("second token after logout-everywhere", 401,
       main.get_current_user, "Bearer " + second, db())

check("unreadable expiry is treated as expired",
      main.parse_stamp("not a date").year, 1970)

# ----------------------------------------------------------- rate limiting
banner("3. RATE LIMITING")

reset_limits()
for i in range(10):
    try:
        main.login(main.LoginIn(email="asha@example.com", password="nope"),
                   req("198.51.100.4"), db())
    except HTTPException as exc:
        if exc.status_code != 401:
            print("  unexpected %s on attempt %d" % (exc.status_code, i + 1))
exc = raises("11th bad login from one IP", 429, main.login,
             main.LoginIn(email="asha@example.com", password="nope"),
             req("198.51.100.4"), db())
check("429 carries Retry-After", "Retry-After" in (exc.headers or {}))

# A different address is not caught by the per-IP bucket, but the per-account
# bucket has also been consumed by those ten attempts, so it is still refused.
raises("same account from a new IP is still throttled", 429, main.login,
       main.LoginIn(email="asha@example.com", password="nope"),
       req("198.51.100.99"), db())

reset_limits()
check("a different account from a new IP is fine",
      main.login(main.LoginIn(email="ravi@example.com", password="secret123"),
                 req("198.51.100.99"), db())["ok"])

check("x-forwarded-for is used over the socket peer",
      main.client_ip(FakeRequest(headers={"x-forwarded-for": "1.2.3.4, 9.9.9.9"},
                                 host="10.0.0.1")), "1.2.3.4")
check("socket peer is used when there is no header",
      main.client_ip(FakeRequest(headers={}, host="10.0.0.1")), "10.0.0.1")

# ---------------------------------------------------------- password reset
banner("4. PASSWORD RESET")

reset_limits()
bg = BackgroundTasks()
out = main.forgot_password(main.ForgotIn(email="nobody@example.com"), bg,
                           req(), db())
unknown_answer = out["message"]
check("unknown address still answers ok", out["ok"])
check("unknown address queues no email", len(bg.tasks), 0)

bg = BackgroundTasks()
out = main.forgot_password(main.ForgotIn(email="asha@example.com"), bg,
                           req(), db())
check("known address gives the identical answer", out["message"],
      unknown_answer)
check("known address queues one email", len(bg.tasks), 1)

task = bg.tasks[0]
check("the queued task is the reset mailer", task.func.__name__,
      "send_password_reset")
RESET_TOKEN = task.args[1]
check("a link was built from APP_URL",
      task.args[2], "https://example.test/app/#reset=%s" % RESET_TOKEN)

rows = db().execute("SELECT token_hash FROM password_resets").fetchall()
check("one reset row exists", len(rows), 1)
check("the raw token is NOT in the database",
      any(r["token_hash"] == RESET_TOKEN for r in rows), False)
check("the stored value is the token's digest",
      rows[0]["token_hash"], main.hash_token(RESET_TOKEN))

# Asking again replaces the outstanding link rather than adding a second.
bg = BackgroundTasks()
main.forgot_password(main.ForgotIn(email="asha@example.com"), bg, req(), db())
SECOND_TOKEN = bg.tasks[0].args[1]
rows = db().execute(
    "SELECT token_hash FROM password_resets WHERE used_at IS NULL").fetchall()
check("asking twice leaves one live link", len(rows), 1)
raises("the superseded token no longer works", 400, main.reset_password,
       main.ResetIn(token=RESET_TOKEN, password="brandnew123"), req(), db())

reset_limits()
raises("a made-up token", 400, main.reset_password,
       main.ResetIn(token="x" * 40, password="brandnew123"), req(), db())

# The real one works, and signs every device out.
reset_limits()
live = main.login(main.LoginIn(email="asha@example.com", password="secret123"),
                  req(), db())["token"]
out = main.reset_password(
    main.ResetIn(token=SECOND_TOKEN, password="brandnew123"), req(), db())
check("reset succeeds", out["ok"])
raises("the session held before the reset", 401,
       main.get_current_user, "Bearer " + live, db())

reset_limits()
raises("the old password", 401, main.login,
       main.LoginIn(email="asha@example.com", password="secret123"), req(), db())
reset_limits()
check("the new password works",
      main.login(main.LoginIn(email="asha@example.com",
                              password="brandnew123"), req(), db())["ok"])

reset_limits()
raises("replaying a used token", 400, main.reset_password,
       main.ResetIn(token=SECOND_TOKEN, password="again12345"), req(), db())

# An expired link is refused even though it was never used.
reset_limits()
bg = BackgroundTasks()
main.forgot_password(main.ForgotIn(email="asha@example.com"), bg, req(), db())
stale = bg.tasks[0].args[1]
conn = db()
conn.execute("UPDATE password_resets SET expires_at = ? WHERE token_hash = ?",
             (main.stamp(main.utcnow() - main.timedelta(minutes=1)),
              main.hash_token(stale)))
conn.commit()
raises("an expired link", 400, main.reset_password,
       main.ResetIn(token=stale, password="again12345"), req(), db())

# A suspended account cannot be reset into.
conn = db()
conn.execute("UPDATE users SET blocked_at = ? WHERE id = ?",
             (main.stamp(main.utcnow()), SHOP_ID))
conn.commit()
reset_limits()
bg = BackgroundTasks()
main.forgot_password(main.ForgotIn(email="ravi@example.com"), bg, req(), db())
check("a suspended account is sent no reset mail", len(bg.tasks), 0)
conn = db()
conn.execute("UPDATE users SET blocked_at = NULL WHERE id = ?", (SHOP_ID,))
conn.commit()

# -------------------------------------------------------- account deletion
banner("5. ACCOUNT DELETION")

reset_limits()
tok = main.login(main.LoginIn(email="asha@example.com",
                              password="brandnew123"), req(), db())["token"]
me = main.get_current_user("Bearer " + tok, db())

# Give the student something to lose first.
conn = db()
conn.execute("INSERT INTO requests (student_id, title, category) "
             "VALUES (?,?,?)", (STUDENT_ID, "Drafting board", "Stationery"))
rid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
conn.execute("INSERT INTO offers (request_id, shop_id, product_name, price) "
             "VALUES (?,?,?,?)", (rid, SHOP_ID, "Board", 450.0))
conn.execute("INSERT INTO messages (request_id, shop_id, sender_id, body) "
             "VALUES (?,?,?,?)", (rid, SHOP_ID, STUDENT_ID, "is it in stock?"))
conn.execute("INSERT INTO notifications (user_id, kind, title) VALUES (?,?,?)",
             (STUDENT_ID, "offer", "New offer"))
conn.execute("INSERT INTO reviews (shop_id, student_id, rating, body) "
             "VALUES (?,?,?,?)", (SHOP_ID, STUDENT_ID, 5, "quick"))
conn.commit()

raises("delete with the wrong password", 403, main.delete_me,
       main.DeleteMeIn(password="not-it"), db(), me)
check("the account survives a refused delete",
      db().execute("SELECT COUNT(*) c FROM users WHERE id=?",
                   (STUDENT_ID,)).fetchone()["c"], 1)

out = main.delete_me(main.DeleteMeIn(password="brandnew123"), db(), me)
check("delete succeeds with the right password", out["deleted"])

for table, where in (("users", "id"), ("sessions", "user_id"),
                     ("requests", "student_id"), ("notifications", "user_id"),
                     ("reviews", "student_id"), ("messages", "sender_id"),
                     ("password_resets", "user_id")):
    n = db().execute("SELECT COUNT(*) c FROM %s WHERE %s = ?" % (table, where),
                     (STUDENT_ID,)).fetchone()["c"]
    check("%s rows are gone" % table, n, 0)

n = db().execute("SELECT COUNT(*) c FROM offers WHERE request_id = ?",
                 (rid,)).fetchone()["c"]
check("offers on the deleted request are gone", n, 0)
n = db().execute("SELECT COUNT(*) c FROM email_log WHERE user_id = ?",
                 (STUDENT_ID,)).fetchone()["c"]
check("the email log is cleared too", n, 0)
raises("the token after deletion", 401,
       main.get_current_user, "Bearer " + tok, db())

# An owner account refuses to delete itself.
bg = BackgroundTasks()
main.register(main.RegisterIn(name="Owner", email="owner@example.com",
                              password="secret123", role="student",
                              is_adult=True), bg, req(), db())
reset_limits()
otok = main.login(main.LoginIn(email="owner@example.com",
                               password="secret123"), req(), db())["token"]
owner = main.get_current_user("Bearer " + otok, db())
check("the owner is recognised as admin", owner["email"], "owner@example.com")
check("is_admin agrees", main.is_admin(owner))
raises("an owner deleting itself", 400, main.delete_me,
       main.DeleteMeIn(password="secret123"), db(), owner)

# ------------------------------------------------------------------ report
banner("RESULT")
print("%d passed, %d failed" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED: %s" % f)
sys.exit(1 if FAIL else 0)
