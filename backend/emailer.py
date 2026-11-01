# -*- coding: utf-8 -*-
"""
emailer.py -- Email notifications for the HyperLocal Student Marketplace.

Drop this file next to main.py. It has NO third-party dependencies
(uses stdlib smtplib + email). Configuration comes from environment
variables or a .env file sitting next to this module.

Notification events
-------------------
  welcome       -> on registration
  new_offer     -> to the student, when a shop submits an offer
  new_request   -> to shops in the same area, when a student posts a request

Design notes
------------
* Every send runs through send_email(), which writes a row to the
  email_log table (sent / failed / skipped / console / duplicate).
* Notification functions take only row IDs, so they are safe to hand to
  FastAPI's BackgroundTasks -- they open their own SQLite connection.
* Sends are serialized behind a lock so a burst of offers does not open
  twenty parallel SMTP connections (Gmail will throttle that).
* A failed email never breaks the API request. It is logged and dropped.
* ASCII only in console output and "Rs." instead of the rupee sign, so
  Windows cp1252 terminals do not raise UnicodeEncodeError.
"""

import os
import ssl
import html
import sqlite3
import smtplib
import threading
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

# --------------------------------------------------------------------------
# Tiny .env loader (so python-dotenv is not required)
# --------------------------------------------------------------------------

def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

DB_PATH = os.getenv("MARKETPLACE_DB", "marketplace.db")
MAX_FANOUT = int(os.getenv("EMAIL_MAX_FANOUT", "50"))
_send_lock = threading.Lock()


def _env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _say(message):
    """print() that cannot crash the server.

    This module's own text is ASCII, but it echoes user data -- a request
    title, a shop name, an email address. A Hindi title or a rupee sign on a
    Windows cp1252 console raises UnicodeEncodeError exactly like Problem 1
    did. So: print normally where the terminal can take it, and fall back to
    an ASCII-folded line where it cannot. The email itself is always sent as
    UTF-8 and keeps the original characters; only this console echo degrades.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        print(message.encode("ascii", "replace").decode("ascii"))


class MailConfig:
    """Snapshot of the SMTP settings, read fresh on every send."""

    def __init__(self):
        self.enabled = _env_bool("EMAIL_ENABLED", True)
        self.console_only = _env_bool("EMAIL_CONSOLE_ONLY", False)
        self.host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.use_ssl = _env_bool("SMTP_SSL", False)          # port 465
        self.use_tls = _env_bool("SMTP_STARTTLS", True)      # port 587
        self.timeout = int(os.getenv("SMTP_TIMEOUT", "20"))
        self.from_email = os.getenv("SMTP_FROM", self.user or "no-reply@localhost")
        self.from_name = os.getenv("SMTP_FROM_NAME", "Student Marketplace")
        self.app_name = os.getenv("APP_NAME", "HyperLocal Student Marketplace")
        self.app_url = os.getenv("APP_URL", "")

    def wants_real_send(self):
        """True when a message should actually be handed to an SMTP server.

        EMAIL_CONSOLE_ONLY is the explicit switch, but there is a second,
        quieter case: the switch is off and no credentials have been filled
        in yet. Attempting a real send then means every mail fails against
        smtp.gmail.com with no password, so a half-configured deploy loses
        welcome mail and -- worse -- password resets.

        Falling back to the console keeps those visible in the service log
        instead, and the log line below says plainly which state we are in.
        """
        if self.console_only:
            return False
        if not self.user and not _env_bool("SMTP_ALLOW_ANONYMOUS", False):
            return False
        return True

    def is_usable(self):
        if not self.enabled:
            return False, "EMAIL_ENABLED is off"
        if not self.wants_real_send():
            return True, ""
        if not self.host:
            return False, "SMTP_HOST not set"
        if not self.from_email:
            return False, "SMTP_FROM not set"
        return True, ""


def get_config():
    return MailConfig()


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------

@contextmanager
def _conn():
    """Open a connection, commit on success, and ALWAYS close it.

    Plain `with sqlite3.connect(...)` commits but does not close, which
    leaks a file handle per email. On Windows those handles hold a lock
    on marketplace.db and eventually produce 'database is locked'.
    """
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_email_schema():
    """Idempotent. Call once at app startup, after your own table setup."""
    with _conn() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        if "notify_email" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN notify_email INTEGER DEFAULT 1"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER REFERENCES users(id),
                to_email   TEXT NOT NULL,
                subject    TEXT NOT NULL,
                kind       TEXT NOT NULL,
                ref        TEXT,
                status     TEXT NOT NULL,
                error      TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_log_ref "
            "ON email_log(kind, ref, to_email)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_email_log_user "
            "ON email_log(user_id, id DESC)"
        )
    _say("[email] schema ready (db=%s)" % DB_PATH)


def _log(to_email, subject, kind, status, ref=None, user_id=None, error=None):
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO email_log (user_id, to_email, subject, kind, ref, "
                "status, error) VALUES (?,?,?,?,?,?,?)",
                (user_id, to_email, subject, kind, ref, status,
                 (error or "")[:500] or None),
            )
    except Exception as exc:                       # never let logging break a send
        _say("[email] could not write email_log: %s" % exc)


def _already_sent(kind, ref, to_email):
    if not ref:
        return False
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM email_log WHERE kind=? AND ref=? AND to_email=? "
            "AND status IN ('sent','console') LIMIT 1",
            (kind, ref, to_email),
        ).fetchone()
    return row is not None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def rs(amount):
    """Format money. 'Rs.' rather than the rupee sign: cp1252-safe."""
    try:
        return "Rs. %s" % format(float(amount), ",.2f")
    except (TypeError, ValueError):
        return "Rs. --"


def _esc(value):
    return html.escape("" if value is None else str(value))


_STYLE_WRAP = (
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    "background:#F3F4F6;padding:24px 12px;"
)
_STYLE_CARD = (
    "max-width:560px;margin:0 auto;background:#ffffff;border-radius:12px;"
    "border:1px solid #E5E7EB;overflow:hidden;"
)
_STYLE_HEAD = (
    "background:#111827;color:#ffffff;padding:18px 24px;"
    "font-size:16px;font-weight:600;"
)
_STYLE_BODY = "padding:24px;color:#111827;font-size:15px;line-height:1.55;"
_STYLE_FOOT = (
    "padding:16px 24px;background:#F9FAFB;color:#6B7280;font-size:12px;"
    "border-top:1px solid #E5E7EB;"
)
_STYLE_TH = (
    "text-align:left;padding:8px 10px;background:#F3F4F6;color:#374151;"
    "font-size:13px;border-bottom:1px solid #E5E7EB;"
)
_STYLE_TD = (
    "padding:8px 10px;border-bottom:1px solid #F3F4F6;font-size:14px;"
)


def _layout(heading, body_html, app_name):
    return (
        '<html><body style="%s"><div style="%s">'
        '<div style="%s">%s</div>'
        '<div style="%s">%s</div>'
        '<div style="%s">You are receiving this because email notifications '
        "are on for your %s account. Turn them off any time from the app "
        "settings.</div>"
        "</div></body></html>"
    ) % (
        _STYLE_WRAP, _STYLE_CARD,
        _STYLE_HEAD, _esc(heading),
        _STYLE_BODY, body_html,
        _STYLE_FOOT, _esc(app_name),
    )


def _rows(pairs):
    out = ['<table style="width:100%;border-collapse:collapse;margin:14px 0;">']
    for label, value in pairs:
        if value in (None, ""):
            continue
        out.append(
            '<tr><th style="%s">%s</th><td style="%s">%s</td></tr>'
            % (_STYLE_TH, _esc(label), _STYLE_TD, _esc(value))
        )
    out.append("</table>")
    return "".join(out)


# --------------------------------------------------------------------------
# Core send
# --------------------------------------------------------------------------

def send_email(to_email, subject, html_body, text_body,
               kind="generic", ref=None, user_id=None, dedupe=True):
    """Send one message. Returns True when it left the machine.

    Never raises: every failure path is logged and returns False.
    """
    cfg = get_config()
    to_email = (to_email or "").strip()

    if not to_email or "@" not in to_email:
        _log(to_email or "(empty)", subject, kind, "skipped", ref, user_id,
             "no valid recipient")
        return False

    if dedupe and _already_sent(kind, ref, to_email):
        _log(to_email, subject, kind, "duplicate", ref, user_id)
        return False

    usable, reason = cfg.is_usable()
    if not usable:
        _log(to_email, subject, kind, "skipped", ref, user_id, reason)
        _say("[email] skipped '%s' to %s (%s)" % (kind, to_email, reason))
        return False

    if not cfg.wants_real_send():
        why = ("EMAIL_CONSOLE_ONLY is on" if cfg.console_only
               else "SMTP_USER is not set, so nothing can be sent yet")
        _say("\n" + "=" * 64)
        _say("[email:console] to      : %s" % to_email)
        _say("[email:console] subject : %s" % subject)
        _say("[email:console] kind    : %s  ref=%s" % (kind, ref))
        _say("[email:console] why     : %s" % why)
        _say("-" * 64)
        _say(text_body)
        _say("=" * 64 + "\n")
        _log(to_email, subject, kind, "console", ref, user_id, why)
        return True

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.from_name, cfg.from_email))
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.from_email.split("@")[-1])
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text_body, charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    server = None
    try:
        with _send_lock:
            if cfg.use_ssl:
                server = smtplib.SMTP_SSL(
                    cfg.host, cfg.port, timeout=cfg.timeout,
                    context=ssl.create_default_context(),
                )
            else:
                server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
                server.ehlo()
                if cfg.use_tls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
            if cfg.user:
                server.login(cfg.user, cfg.password)
            server.send_message(msg)
        _log(to_email, subject, kind, "sent", ref, user_id)
        _say("[email] sent '%s' to %s" % (kind, to_email))
        return True
    except Exception as exc:
        detail = "%s: %s" % (type(exc).__name__, exc)
        _log(to_email, subject, kind, "failed", ref, user_id, detail)
        _say("[email] FAILED '%s' to %s -- %s" % (kind, to_email, detail))
        return False
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Notification builders -- these take IDs and are BackgroundTasks-safe
# --------------------------------------------------------------------------

def _wants_email(row):
    if row is None:
        return False
    try:
        flag = row["notify_email"]
    except (IndexError, KeyError):
        return True
    return flag is None or int(flag) == 1


def send_welcome(user_id):
    cfg = get_config()
    with _conn() as conn:
        user = conn.execute(
            "SELECT id, name, email, role, area, shop_name, notify_email "
            "FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if user is None or not _wants_email(user):
        return

    if user["role"] == "shop":
        intro = (
            "Your shop is now listed. When a student in your area posts "
            "something they need, we will email you so you can send an offer "
            "before anyone else does."
        )
    else:
        intro = (
            "Post what you need with your specs and budget. Local shops will "
            "reply with offers, and we will email you every time a new one "
            "arrives -- with the price compared against the best offer so far."
        )

    details = _rows([
        ("Name", user["name"]),
        ("Role", "Shop" if user["role"] == "shop" else "Student"),
        ("Shop", user["shop_name"]),
        ("Area", user["area"]),
    ])

    body = "<p>Hello %s,</p><p>%s</p>%s" % (
        _esc(user["name"]), _esc(intro), details)
    text = "Hello %s,\n\n%s\n\nRole: %s\nArea: %s\n" % (
        user["name"], intro,
        "Shop" if user["role"] == "shop" else "Student",
        user["area"] or "-")

    send_email(
        user["email"],
        "Welcome to %s" % cfg.app_name,
        _layout("Welcome to %s" % cfg.app_name, body, cfg.app_name),
        text,
        kind="welcome",
        ref="user:%s" % user["id"],
        user_id=user["id"],
    )


def notify_new_offer(offer_id):
    """Email the student who owns the request that a new offer arrived."""
    cfg = get_config()
    with _conn() as conn:
        offer = conn.execute(
            """
            SELECT o.id, o.request_id, o.product_name, o.specs, o.price, o.note,
                   s.name AS shop_contact, s.shop_name, s.area
            FROM offers o
            JOIN users s ON s.id = o.shop_id
            WHERE o.id = ?
            """, (offer_id,)
        ).fetchone()
        if offer is None:
            return

        req = conn.execute(
            """
            SELECT r.id, r.title, r.category, r.budget,
                   u.id AS student_id, u.name AS student_name,
                   u.email AS student_email, u.notify_email
            FROM requests r
            JOIN users u ON u.id = r.student_id
            WHERE r.id = ?
            """, (offer["request_id"],)
        ).fetchone()
        if req is None or not _wants_email(req):
            return

        stats = conn.execute(
            "SELECT COUNT(*) AS n, MIN(price) AS best FROM offers "
            "WHERE request_id=?", (offer["request_id"],)
        ).fetchone()

    total = stats["n"] or 1
    best = stats["best"]
    price = float(offer["price"])
    is_best = best is not None and price <= float(best) + 1e-9
    shop_label = offer["shop_name"] or offer["shop_contact"] or "A local shop"

    if is_best:
        verdict = "This is the lowest offer on your request so far."
        verdict_style = "color:#065F46;background:#D1FAE5;"
    else:
        verdict = "This is %s more than the best offer so far (%s)." % (
            rs(price - float(best)), rs(best))
        verdict_style = "color:#92400E;background:#FEF3C7;"

    budget_note = ""
    if req["budget"] is not None:
        within = price <= float(req["budget"])
        budget_note = "<p style='margin:4px 0;color:%s;'>%s your budget of %s.</p>" % (
            "#065F46" if within else "#B91C1C",
            "Within" if within else "Over",
            rs(req["budget"]),
        )

    details = _rows([
        ("Shop", shop_label),
        ("Area", offer["area"]),
        ("Product", offer["product_name"]),
        ("Specs", offer["specs"]),
        ("Price", rs(price)),
        ("Note", offer["note"]),
    ])

    body = (
        "<p>Hello %s,</p>"
        "<p><strong>%s</strong> sent an offer on your request "
        "<strong>%s</strong>.</p>"
        "%s"
        "<div style='%spadding:10px 12px;border-radius:8px;font-size:14px;'>%s</div>"
        "%s"
        "<p style='color:#6B7280;font-size:13px;'>You now have %d offer%s on "
        "this request. Open the app and use <em>View Offers &amp; Compare</em> "
        "to see the full comparison table.</p>"
    ) % (
        _esc(req["student_name"]), _esc(shop_label), _esc(req["title"]),
        details, verdict_style, _esc(verdict), budget_note,
        total, "" if total == 1 else "s",
    )

    text = (
        "Hello %s,\n\n"
        "%s sent an offer on your request \"%s\".\n\n"
        "Product : %s\nSpecs   : %s\nPrice   : %s\nShop    : %s (%s)\nNote    : %s\n\n"
        "%s\n\n"
        "You now have %d offer(s) on this request. Open the app and use "
        "'View Offers & Compare' for the full table.\n"
    ) % (
        req["student_name"], shop_label, req["title"],
        offer["product_name"], offer["specs"] or "-", rs(price),
        shop_label, offer["area"] or "-", offer["note"] or "-",
        verdict, total,
    )

    subject = "New offer on '%s' -- %s%s" % (
        req["title"], rs(price), " (best price)" if is_best else "")

    send_email(
        req["student_email"], subject,
        _layout("New offer received", body, cfg.app_name), text,
        kind="new_offer",
        ref="offer:%s" % offer["id"],
        user_id=req["student_id"],
    )


def notify_new_request(request_id):
    """Email shops in the same area that a student posted a request."""
    cfg = get_config()
    with _conn() as conn:
        req = conn.execute(
            """
            SELECT r.id, r.title, r.category, r.description, r.specs, r.budget,
                   u.name AS student_name, u.area AS student_area
            FROM requests r
            JOIN users u ON u.id = r.student_id
            WHERE r.id = ?
            """, (request_id,)
        ).fetchone()
        if req is None:
            return

        area = (req["student_area"] or "").strip()
        if area:
            shops = conn.execute(
                "SELECT id, name, email, shop_name, notify_email FROM users "
                "WHERE role='shop' AND TRIM(LOWER(COALESCE(area,''))) = ? "
                "LIMIT ?", (area.lower(), MAX_FANOUT)
            ).fetchall()
        else:
            shops = conn.execute(
                "SELECT id, name, email, shop_name, notify_email FROM users "
                "WHERE role='shop' LIMIT ?", (MAX_FANOUT,)
            ).fetchall()

    if not shops:
        _say("[email] no shops to notify for request %s (area=%s)"
             % (request_id, area or "-"))
        return

    details = _rows([
        ("Category", req["category"]),
        ("Specs", req["specs"]),
        ("Details", req["description"]),
        ("Budget", rs(req["budget"]) if req["budget"] is not None else None),
        ("Area", area or None),
    ])

    for shop in shops:
        if not _wants_email(shop):
            continue
        greeting = shop["shop_name"] or shop["name"]
        body = (
            "<p>Hello %s,</p>"
            "<p>A student in your area is looking for "
            "<strong>%s</strong>.</p>%s"
            "<p>Open the app, go to <em>Student Requests</em>, and submit your "
            "price. The student sees every offer side by side, so a sharp "
            "quote and clear specs win the order.</p>"
        ) % (_esc(greeting), _esc(req["title"]), details)

        text = (
            "Hello %s,\n\n"
            "A student in your area is looking for: %s\n\n"
            "Category : %s\nSpecs    : %s\nDetails  : %s\nBudget   : %s\n\n"
            "Open the app, go to 'Student Requests', and submit your offer.\n"
        ) % (
            greeting, req["title"], req["category"], req["specs"] or "-",
            req["description"] or "-",
            rs(req["budget"]) if req["budget"] is not None else "not stated",
        )

        send_email(
            shop["email"],
            "Student request in your area: %s" % req["title"],
            _layout("New request near you", body, cfg.app_name), text,
            kind="new_request",
            ref="request:%s" % req["id"],
            user_id=shop["id"],
        )


def send_password_reset(user_id, token, link, ttl_minutes=60):
    """Email a password reset link.

    Three things this deliberately does differently from every other mail
    in here:

    * dedupe is off. The dedupe key is kind+ref+recipient, and a second
      reset request is a legitimate repeat -- the first mail went to spam,
      or the link expired. Deduping would silently swallow it.
    * notify_email is ignored. Somebody who turned off offer alerts has
      not opted out of being able to get back into their own account.
    * ref carries no token. The email_log is readable from the owner
      dashboard, and a reset token sitting in a log is a working key.
    """
    cfg = get_config()
    with _conn() as conn:
        user = conn.execute(
            "SELECT id, name, email FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if user is None:
        return False

    # Without APP_URL there is no page to link to, so the mail carries the
    # token itself and says what to do with it. Worse to read, still works.
    if link:
        action_html = (
            "<p style='margin:20px 0;'>"
            "<a href='%s' style='background:#111827;color:#ffffff;"
            "padding:12px 20px;border-radius:8px;text-decoration:none;"
            "display:inline-block;font-weight:600;'>Choose a new password</a>"
            "</p>"
            "<p style='color:#6B7280;font-size:13px;word-break:break-all;'>"
            "If the button does not work, paste this into your browser:<br>%s</p>"
        ) % (_esc(link), _esc(link))
        action_text = "Open this link to choose a new password:\n%s\n" % link
    else:
        action_html = (
            "<p>Open the app, choose <strong>Forgot password</strong>, and "
            "enter this code:</p>"
            "<p style='font-family:monospace;font-size:15px;background:#F3F4F6;"
            "padding:12px;border-radius:8px;word-break:break-all;'>%s</p>"
        ) % _esc(token)
        action_text = ("Open the app, choose 'Forgot password', and enter "
                       "this code:\n\n%s\n") % token

    body = (
        "<p>Hello %s,</p>"
        "<p>Somebody asked to reset the password for this account. The link "
        "below works once and expires in %d minutes.</p>"
        "%s"
        "<p style='color:#6B7280;font-size:13px;'>If this was not you, you "
        "can ignore this email -- your password has not changed, and nobody "
        "can read it from this message.</p>"
    ) % (_esc(user["name"]), int(ttl_minutes), action_html)

    text = (
        "Hello %s,\n\n"
        "Somebody asked to reset the password for this account. This works "
        "once and expires in %d minutes.\n\n"
        "%s\n"
        "If this was not you, ignore this email. Your password has not "
        "changed.\n"
    ) % (user["name"], int(ttl_minutes), action_text)

    return send_email(
        user["email"],
        "Reset your %s password" % cfg.app_name,
        _layout("Password reset", body, cfg.app_name), text,
        kind="password_reset", ref=None, user_id=user["id"], dedupe=False,
    )


def send_test(user_id):
    """Used by POST /api/notifications/test to verify SMTP settings."""
    cfg = get_config()
    with _conn() as conn:
        user = conn.execute(
            "SELECT id, name, email FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if user is None:
        return False

    body = (
        "<p>Hello %s,</p><p>Your SMTP settings are working. This is a test "
        "message from %s.</p>%s"
    ) % (
        _esc(user["name"]), _esc(cfg.app_name),
        _rows([("SMTP host", cfg.host), ("Port", cfg.port),
               ("From", cfg.from_email), ("TLS", "on" if cfg.use_tls else "off")]),
    )
    text = ("Hello %s,\n\nYour SMTP settings are working. Sent from %s via "
            "%s:%s.\n") % (user["name"], cfg.app_name, cfg.host, cfg.port)

    return send_email(
        user["email"], "Test email from %s" % cfg.app_name,
        _layout("Test email", body, cfg.app_name), text,
        kind="test", ref=None, user_id=user["id"], dedupe=False,
    )


# --------------------------------------------------------------------------
# Manual check:  python emailer.py your@email.com
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    cfg = get_config()
    _say("host=%s port=%s tls=%s ssl=%s from=%s console_only=%s enabled=%s"
         % (cfg.host, cfg.port, cfg.use_tls, cfg.use_ssl, cfg.from_email,
            cfg.console_only, cfg.enabled))
    init_email_schema()

    if len(sys.argv) > 1:
        target = sys.argv[1]
        ok = send_email(
            target, "Test email from %s" % cfg.app_name,
            _layout("Test email",
                    "<p>If you can read this, SMTP is configured correctly.</p>",
                    cfg.app_name),
            "If you can read this, SMTP is configured correctly.\n",
            kind="test", dedupe=False,
        )
        _say("result: %s" % ("OK" if ok else "FAILED -- see message above"))
    else:
        print("Pass an address to send a test: python emailer.py you@example.com")
