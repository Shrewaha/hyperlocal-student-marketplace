# -*- coding: utf-8 -*-
"""
HyperLocal Student Marketplace -- FastAPI backend.

A two-sided marketplace: students post what they need, local shops reply
with offers, and the student gets a price comparison with the best deal
flagged. Pure JSON REST API -- no HTML, no templates. The JavaFX desktop
app in ../frontend is one client; anything that speaks HTTP is another.

Run:
    pip install -r ../requirements.txt
    uvicorn main:app --reload
    # interactive docs: http://localhost:8000/docs

Email notifications live in emailer.py and are queued as background tasks,
so a slow or broken SMTP server can never delay or fail an API response.
"""

import os
import json
import time
import base64
import secrets
import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import (FastAPI, Depends, Header, HTTPException, BackgroundTasks,
                     Query, Request)
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

import emailer

DB_PATH = os.getenv("MARKETPLACE_DB", "marketplace.db")

CATEGORIES = [
    "Electronics", "Stationery", "Lab Components", "Books",
    "Computer Parts", "Tools", "Other",
]

ROLES = ("student", "shop")

# Who may administer the marketplace, read from the environment rather than
# stored as a role on an account.
#
# Nothing in the API can grant this, so there is no "make me an admin"
# endpoint to find, no row to tamper with, and revoking it is changing a
# setting rather than hoping a database update ran. On Render this is set in
# the dashboard: ADMIN_EMAILS=you@example.com
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
}

# The youngest age that may hold an account.
#
# Set to 14 deliberately. Note what it means: the DPDP Act treats anyone
# under 18 as a child and expects verifiable parental consent before their
# data is held, and this marketplace collects none. Accounts between 14 and
# 17 are therefore held without that consent. Raising this to 18 is a
# one-line change here, and an environment variable on a running service.
MIN_SIGNUP_AGE = int(os.getenv("MIN_SIGNUP_AGE", "14"))

# Nothing deep: an upper bound so a typo lands as a refusal rather than as
# a 900-year-old shopkeeper.
MAX_SIGNUP_AGE = 120

# How long a sign-in lasts without being used. The window slides: every
# authenticated request pushes it out again, so somebody who opens the app
# each week is never signed out, while a phone that stops being used goes
# cold on its own.
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))

# How long a password-reset link stays valid. Short on purpose -- the link
# is a way into the account, and it is read from an inbox minutes after it
# is asked for, not days.
RESET_TTL_MINUTES = int(os.getenv("RESET_TTL_MINUTES", "60"))

# Where the browser client lives, used to build the link inside the reset
# email. Without it the email carries the raw token and tells the reader
# where to paste it, which still works but reads like a ransom note.
APP_URL = os.getenv("APP_URL", "").rstrip("/")

app = FastAPI(
    title="HyperLocal Student Marketplace",
    description="Students post what they need. Local shops compete on price.",
    version="1.1.0",
)

# The JavaFX client is not a browser and sends no Origin, but leaving this
# open means a web or Android frontend can be added without touching the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==========================================================================
# Database
# ==========================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    email      TEXT UNIQUE NOT NULL,
    password   TEXT NOT NULL,              -- pbkdf2_sha256$iterations$salt$hash
    role       TEXT NOT NULL CHECK(role IN ('student','shop')),
    area       TEXT,
    shop_name  TEXT,
    avatar     BLOB,
    avatar_type TEXT,
    blocked_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id     INTEGER NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    description TEXT,
    specs       TEXT,
    price       REAL NOT NULL,
    stock       INTEGER DEFAULT 1,
    barcode     TEXT,
    image       BLOB,
    image_type  TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER NOT NULL REFERENCES users(id),
    title       TEXT NOT NULL,
    category    TEXT NOT NULL,
    description TEXT,
    specs       TEXT,
    budget      REAL,
    status      TEXT DEFAULT 'open',
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS offers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   INTEGER NOT NULL REFERENCES requests(id),
    shop_id      INTEGER NOT NULL REFERENCES users(id),
    product_name TEXT NOT NULL,
    specs        TEXT,
    price        REAL NOT NULL,
    note         TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- A signed-in device. expires_at is what stops a token being a permanent
-- key to the account: a phone that is sold, lost or lent stops working on
-- its own, without the owner having to think of revoking anything.
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    expires_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- A pending "I forgot my password" link.
--
-- The token is stored as a SHA-256 digest, never in the clear. A reset link
-- is a bearer credential for the whole account, so a leaked database should
-- not hand over a working one -- the same reasoning that keeps passwords
-- hashed. used_at makes the link single-use: clicking an old link from an
-- inbox after the password has already been changed does nothing.
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    expires_at DATETIME NOT NULL,
    used_at    DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- A conversation is a request paired with one shop. Two shops answering the
-- same request are two separate threads, and the student is whoever owns the
-- request, so the pair alone identifies both sides.
-- Barcodes the marketplace has learned, shared by every shop.
--
-- The first shop to scan a code types the name once; everyone who scans it
-- afterwards gets it filled in. A single shop's app could never do this --
-- it only works because there are many shops behind one catalogue, and it
-- gets faster the more of them there are.
CREATE TABLE IF NOT EXISTS barcodes (
    code       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT,
    specs      TEXT,
    source     TEXT NOT NULL DEFAULT 'shop',   -- shop | openfoodfacts
    seen       INTEGER NOT NULL DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- A student's verdict on a shop. One per pair: a second opinion from the
-- same student replaces the first rather than stacking, so a shop cannot be
-- buried or inflated by one person posting repeatedly.
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id    INTEGER NOT NULL REFERENCES users(id),
    student_id INTEGER NOT NULL REFERENCES users(id),
    rating     INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    body       TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(shop_id, student_id)
);

-- What has happened that someone should know about. Written at the moment
-- the thing happens, so the feed is a record rather than a query that has to
-- reconstruct history from offers, messages and requests every time.
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    kind       TEXT NOT NULL,          -- offer | message | request | welcome
    title      TEXT NOT NULL,
    body       TEXT,
    request_id INTEGER,
    shop_id    INTEGER,
    read_at    DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES requests(id),
    shop_id    INTEGER NOT NULL REFERENCES users(id),
    sender_id  INTEGER NOT NULL REFERENCES users(id),
    body       TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- How far each person has read in each thread. Storing the furthest message
-- id rather than a count means unread stays correct no matter how many
-- messages arrive between visits.
CREATE TABLE IF NOT EXISTS thread_reads (
    request_id   INTEGER NOT NULL,
    shop_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    last_read_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (request_id, shop_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_offers_request  ON offers(request_id, price);
CREATE INDEX IF NOT EXISTS idx_requests_student ON requests(student_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_products_shop   ON products(shop_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(request_id, shop_id, id);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_shop ON reviews(shop_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_resets_user ON password_resets(user_id);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """Request-scoped connection. Always closed, even on an exception."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # Added after the first release, so existing databases need it too.
        # Idempotent: checking the columns is cheaper than catching the error.
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        if "phone" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        for col, decl in (("address", "TEXT"), ("lat", "REAL"), ("lng", "REAL"),
                          ("about", "TEXT"), ("avatar", "BLOB"),
                          ("avatar_type", "TEXT"), ("blocked_at", "DATETIME"),
                          # The age given at signup. A number rather than the
                          # yes/no tick this replaces, because "17" is an
                          # answer the marketplace may need to act on and
                          # "not an adult" is not.
                          ("age", "INTEGER"),
                          # From the release before this one, which asked
                          # only whether the holder was 18 or over. Age
                          # supersedes it and nothing writes it any more.
                          # Kept because dropping a column in SQLite means
                          # rebuilding the table, and an unused column is
                          # cheaper than that.
                          ("adult_confirmed_at", "DATETIME")):
            if col not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN %s %s" % (col, decl))
        # Sessions predate expiry: an existing database has tokens with no
        # expires_at. They are given one from now rather than being deleted,
        # so upgrading does not sign everybody out.
        scols = [r["name"] for r in conn.execute("PRAGMA table_info(sessions)")]
        if "expires_at" not in scols:
            conn.execute("ALTER TABLE sessions ADD COLUMN expires_at DATETIME")
            conn.execute(
                "UPDATE sessions SET expires_at = datetime('now', ?) "
                "WHERE expires_at IS NULL", ("+%d days" % SESSION_TTL_DAYS,))
        pcols = [r["name"] for r in conn.execute("PRAGMA table_info(products)")]
        if "barcode" not in pcols:
            conn.execute("ALTER TABLE products ADD COLUMN barcode TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_products_barcode "
                "ON products(barcode)")
        if "image" not in pcols:
            # The bytes themselves, not a path: the host's disk is wiped on
            # restart, so a file on it would outlive nothing. Kept out of the
            # listing JSON and served from its own endpoint instead.
            conn.execute("ALTER TABLE products ADD COLUMN image BLOB")
            conn.execute("ALTER TABLE products ADD COLUMN image_type TEXT")
        conn.commit()
    finally:
        conn.close()
    emailer.init_email_schema()      # adds users.notify_email + email_log
    print("[db] ready (%s)" % DB_PATH)


init_db()


# ==========================================================================
# Passwords and tokens
# ==========================================================================
#
# Plain SHA-256 is one fast hash per guess, so a leaked database is a
# wordlist away from every password. PBKDF2 with a per-user salt is in the
# standard library and costs nothing extra to use.

PBKDF2_ITERATIONS = 240_000


# SQLite's CURRENT_TIMESTAMP writes UTC as "YYYY-MM-DD HH:MM:SS" with no zone
# marker. These three keep every comparison in that same shape, so an expiry
# written by SQL and one written by Python are the same kind of thing.

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_stamp(value) -> datetime:
    """Read a SQLite timestamp back. Unreadable means expired, not crashed."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip().replace("T", " ")
    for shape in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:26], shape).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # A row whose expiry cannot be read is treated as long gone. The other
    # way round -- unreadable means valid forever -- is the bug this whole
    # change exists to remove.
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITERATIONS, salt, digest)


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations),
    ).hex()
    return secrets.compare_digest(candidate, digest)   # constant time


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Digest a reset token for storage.

    No salt and no iterations, unlike a password: this is 32 bytes of
    cryptographic randomness, not something a person chose, so there is
    nothing to guess and nothing a rainbow table could hold. The digest
    exists so that a copy of the database is not a pile of working reset
    links.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ==========================================================================
# Rate limiting
# ==========================================================================
#
# Without this, /api/login answers as fast as the network allows and a weak
# password falls in minutes. The counters live in memory rather than in the
# database: one uvicorn worker serves this app, and a login attempt should
# not cost a disk write. A restart clears them, which matters less than it
# sounds -- an online guessing run takes hours and deploys do not.
#
# Move this to Redis or a table the day the service runs more than one
# worker, because until then each worker would count separately.

_rate_lock = threading.Lock()
_rate_hits: dict = {}


def rate_limit(bucket: str, limit: int, window_seconds: int) -> None:
    """Allow `limit` hits per `window_seconds` for `bucket`, else 429.

    A sliding window kept as a list of timestamps. The lists are tiny -- at
    most `limit` entries -- and buckets that go quiet are dropped on the
    next sweep, so this cannot grow without bound.
    """
    now = time.monotonic()
    cutoff = now - window_seconds
    with _rate_lock:
        hits = [t for t in _rate_hits.get(bucket, ()) if t > cutoff]
        # Occasional sweep so abandoned buckets (one failed login from an IP
        # never seen again) do not accumulate for the life of the process.
        if len(_rate_hits) > 2048:
            for key in [k for k, v in _rate_hits.items() if not v or max(v) < cutoff]:
                _rate_hits.pop(key, None)
        if len(hits) >= limit:
            retry = int(window_seconds - (now - min(hits))) + 1
            _rate_hits[bucket] = hits
            raise HTTPException(
                429, "Too many attempts. Try again in %d seconds." % retry,
                headers={"Retry-After": str(retry)},
            )
        hits.append(now)
        _rate_hits[bucket] = hits


def client_ip(request: Request) -> str:
    """The caller's address, as far as it can be trusted.

    Render terminates TLS at a proxy, so request.client.host is the proxy
    for every caller and rate limiting on it would throttle the whole
    marketplace at once. The left-most entry of X-Forwarded-For is the
    original client. A caller can forge that header, so this is a civility
    measure against brute force, not an access control.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


# ==========================================================================
# Auth dependencies
# ==========================================================================

def get_current_user(authorization: Optional[str] = Header(None),
                     conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    row = conn.execute(
        "SELECT u.*, s.expires_at AS session_expires_at "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ?", (token,)
    ).fetchone()
    if row is None:
        raise HTTPException(401, "Invalid or expired token")

    # An expired token is deleted rather than merely refused, so a stale row
    # does not sit in the table forever waiting to be swept.
    expires_at = row["session_expires_at"]
    if expires_at is not None and parse_stamp(expires_at) <= utcnow():
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        raise HTTPException(401, "Your session has expired. Please sign in again.")

    # Slide the window forward. Written only when it would move by more than
    # an hour: otherwise every authenticated request costs a disk write, and
    # on SQLite that is a write lock taken on the whole database.
    if expires_at is not None:
        fresh = utcnow() + timedelta(days=SESSION_TTL_DAYS)
        if fresh - parse_stamp(expires_at) > timedelta(hours=1):
            conn.execute("UPDATE sessions SET expires_at = ? WHERE token = ?",
                         (stamp(fresh), token))
            conn.commit()

    user = dict(row)
    user.pop("session_expires_at", None)
    # Checked on every request, not only at sign-in. Blocking somebody who is
    # already signed in has to take effect now; otherwise their existing
    # token keeps working until they happen to sign out.
    if user.get("blocked_at") is not None:
        raise HTTPException(403, "This account has been suspended")
    return user


def reset_link(token: str) -> str:
    """The URL that lands on the reset form, when one can be built."""
    if not APP_URL:
        return ""
    return "%s/#reset=%s" % (APP_URL, token)


def purge_user(conn: sqlite3.Connection, user_id: int) -> None:
    """
    Remove an account and everything that belonged to it.

    Deleted in dependency order, because the schema has foreign keys on and
    SQLite will refuse a parent whose children are still there. Offers and
    requests go too: an offer by a shop that no longer exists would leave a
    comparison table quoting a price nobody stands behind.

    One function so that a person deleting their own account and an owner
    removing somebody both clear the same tables. Two copies of this list
    would drift, and the half that got forgotten would be the half holding
    personal data.

    Does not commit -- the caller decides what else belongs in the same
    transaction.
    """
    conn.execute("DELETE FROM reviews WHERE shop_id = ? OR student_id = ?",
                 (user_id, user_id))
    conn.execute("DELETE FROM messages WHERE sender_id = ? OR shop_id = ?",
                 (user_id, user_id))
    conn.execute("DELETE FROM thread_reads WHERE user_id = ? OR shop_id = ?",
                 (user_id, user_id))
    conn.execute("DELETE FROM notifications WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM offers WHERE shop_id = ?", (user_id,))
    conn.execute("DELETE FROM products WHERE shop_id = ?", (user_id,))
    # A student's requests take their offers with them.
    conn.execute(
        "DELETE FROM offers WHERE request_id IN "
        "(SELECT id FROM requests WHERE student_id = ?)", (user_id,))
    conn.execute("DELETE FROM requests WHERE student_id = ?", (user_id,))
    conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    # The email log holds an address and what was sent to it, so it is
    # personal data and goes with the account. Wrapped because the table
    # belongs to emailer and an older database may not have it yet.
    try:
        conn.execute("DELETE FROM email_log WHERE user_id = ?", (user_id,))
    except sqlite3.OperationalError:
        pass
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def is_admin(user: dict) -> bool:
    return (user.get("email") or "").lower() in ADMIN_EMAILS


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if not is_admin(user):
        # Deliberately the same answer a non-existent route would give, so
        # that probing for an admin area tells an attacker nothing.
        raise HTTPException(404, "Not found")
    return user


def require_student(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "student":
        raise HTTPException(403, "This endpoint is for student accounts")
    return user


def require_shop(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "shop":
        raise HTTPException(403, "This endpoint is for shop accounts")
    return user


def normalize_area(value: Optional[str]) -> Optional[str]:
    """
    Tidy an area name for storage, keeping the capitalisation as typed.

    Area is the key that decides which shops hear about a student's request,
    and it arrives as free text. "Civil  Lines" and "Civil Lines " are the
    same place to a person and were two different places to the database, so
    a student could post a request that no shop ever saw and get no hint why.
    Collapsing the whitespace here means the lowercase comparison used for
    matching lines up for everything but a genuine misspelling -- and the
    /api/areas list exists so people pick an existing spelling instead of
    inventing one.
    """
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def area_key(value: Optional[str]) -> str:
    """The comparison form: what two areas have to share to count as one."""
    return (normalize_area(value) or "").lower()


def canonical_area(conn: sqlite3.Connection, area: Optional[str]) -> Optional[str]:
    """
    The established spelling of an area, for showing back to the user.

    Someone who typed "civil lines" should still read "Civil Lines" in the
    confirmation, because that is the name the place goes by here. Shops are
    weighted first: theirs is the spelling a request has to match.
    """
    key = area_key(area)
    if not key:
        return normalize_area(area)
    row = conn.execute(
        "SELECT area FROM users "
        "WHERE TRIM(LOWER(COALESCE(area,''))) = ? "
        "GROUP BY area "
        "ORDER BY SUM(CASE WHEN role = 'shop' THEN 1 ELSE 0 END) DESC, COUNT(*) DESC "
        "LIMIT 1", (key,)
    ).fetchone()
    return row["area"] if row else normalize_area(area)


def count_shops_in_area(conn: sqlite3.Connection, area: Optional[str]) -> int:
    key = area_key(area)
    if not key:
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM users "
        "WHERE role = 'shop' AND blocked_at IS NULL "
        "AND TRIM(LOWER(COALESCE(area,''))) = ?", (key,)
    ).fetchone()
    return int(row["n"]) if row else 0


def notify(conn: sqlite3.Connection, user_id: int, kind: str, title: str,
           body: Optional[str] = None, request_id: Optional[int] = None,
           shop_id: Optional[int] = None) -> None:
    """
    Record something worth knowing about.

    Written as the event happens rather than worked out later from offers,
    messages and requests: a feed assembled on read would have to re-derive
    history on every poll, and could not remember what you had already seen.
    Never allowed to fail the thing that caused it -- an unrecorded
    notification is a smaller problem than an offer that would not send.
    """
    try:
        conn.execute(
            "INSERT INTO notifications (user_id, kind, title, body, request_id, shop_id) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, kind, title, body, request_id, shop_id),
        )
    except Exception as exc:                      # pragma: no cover
        print("[notify] could not record %s for user %s: %s" % (kind, user_id, exc))


def normalize_phone(value: Optional[str]) -> Optional[str]:
    """
    Keep the digits and a leading +, drop the spaces, dashes and brackets
    people type. Stored this way so a tel: or WhatsApp link built from it
    works without the client having to clean it up first.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    plus = raw.startswith("+")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    return ("+" if plus else "") + digits


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def decode_data_url(value: Optional[str]):
    """
    Turn "data:image/jpeg;base64,..." into (bytes, media type).

    Returns (None, None) for no image. Raises for anything that is not one of
    the three formats every browser can both produce and display -- a stored
    SVG would be a script this server hands back to whoever asks for it.
    """
    if value is None or not value.strip():
        return None, None
    head, _, payload = value.partition(",")
    if not payload or not head.startswith("data:") or ";base64" not in head:
        raise HTTPException(400, "Image must be a base64 data URL")
    media = head[5:].split(";", 1)[0].lower()
    if media not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, "Image must be a JPEG, PNG or WebP")
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        raise HTTPException(400, "Image is not valid base64")
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image is too large; keep it under 2 MB")
    return raw, media


def public_user(user: dict) -> dict:
    """Never let the password hash out of the building."""
    return {
        "id": user["id"], "name": user["name"], "email": user["email"],
        "role": user["role"], "area": user["area"],
        "shop_name": user["shop_name"],
        "phone": user.get("phone"),
        "address": user.get("address"),
        "lat": user.get("lat"),
        "lng": user.get("lng"),
        "about": user.get("about"),
        "age": user.get("age"),
        "has_avatar": user.get("avatar") is not None,
        "is_admin": is_admin(user),
        "notify_email": bool(user.get("notify_email", 1) in (1, None)),
    }


# ==========================================================================
# Schemas
# ==========================================================================

class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=6, max_length=200)
    role: str
    area: Optional[str] = Field(default=None, max_length=120)
    shop_name: Optional[str] = Field(default=None, max_length=160)
    # Shops only. It is shown to a student on that shop's offers, which is
    # how the two actually reach each other once a price is agreed.
    phone: Optional[str] = Field(default=None, max_length=32)
    # Optional in the schema so that leaving it out produces the sentence
    # below rather than pydantic's "Input should be a valid integer", which
    # a shopkeeper cannot act on. The endpoint is what insists on it.
    age: Optional[int] = Field(default=None, ge=0, le=MAX_SIGNUP_AGE)


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str = Field(min_length=16, max_length=200)
    password: str = Field(min_length=6, max_length=200)


class DeleteMeIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class ProfileIn(BaseModel):
    """
    Editing a profile, which is not the same shape as creating one. Every
    field is optional because the client sends only what changed, and None
    means "leave this alone" rather than "clear it" -- clearing is done by
    sending an empty string.
    """
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    area: Optional[str] = Field(default=None, max_length=120)
    shop_name: Optional[str] = Field(default=None, max_length=160)
    phone: Optional[str] = Field(default=None, max_length=32)
    # Editable because a number typed once into a signup form is a number
    # typed wrong sometimes, and an account stuck at the wrong age with no
    # way to fix it is the password-reset problem in miniature.
    age: Optional[int] = Field(default=None, ge=0, le=MAX_SIGNUP_AGE)
    address: Optional[str] = Field(default=None, max_length=400)
    about: Optional[str] = Field(default=None, max_length=600)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)
    # Removing a pin cannot be expressed by leaving lat and lng out, because
    # absent means untouched. It needs saying outright.
    clear_location: bool = False
    # A data URL, already shrunk by the browser. Same reasoning as a product
    # photograph; an avatar is smaller again.
    avatar: Optional[str] = Field(default=None, max_length=3_000_000)


class ProductEdit(BaseModel):
    """
    Correcting a listing, which is not the same shape as creating one. Every
    field optional, None meaning untouched -- a shopkeeper fixing a price
    should not have to retype the specs to keep them.
    """
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    category: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=2000)
    specs: Optional[str] = Field(default=None, max_length=1000)
    price: Optional[float] = Field(default=None, ge=0)
    stock: Optional[int] = Field(default=None, ge=0)
    image: Optional[str] = Field(default=None, max_length=3_000_000)
    barcode: Optional[str] = Field(default=None, max_length=32)


class ReviewIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    body: Optional[str] = Field(default=None, max_length=1000)


class ShopIn(BaseModel):
    """An owner registering a shop on its behalf."""
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=6, max_length=200)
    shop_name: str = Field(min_length=1, max_length=160)
    area: Optional[str] = Field(default=None, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=32)


class MessageIn(BaseModel):
    request_id: int
    # Which shop's thread. A shop sending to a student may leave this out --
    # the thread is its own.
    shop_id: Optional[int] = None
    body: str = Field(min_length=1, max_length=2000)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RequestIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category: str
    description: Optional[str] = Field(default=None, max_length=2000)
    specs: Optional[str] = Field(default=None, max_length=1000)
    budget: Optional[float] = Field(default=None, ge=0)


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: str
    description: Optional[str] = Field(default=None, max_length=2000)
    specs: Optional[str] = Field(default=None, max_length=1000)
    price: float = Field(ge=0)
    stock: int = Field(default=1, ge=0)
    # A data URL, as produced by the browser after it has shrunk the picture:
    # "data:image/jpeg;base64,...". Generous ceiling -- the client aims for
    # around a tenth of this, and anything larger is a client that skipped
    # the resize rather than a photograph that needed the room.
    image: Optional[str] = Field(default=None, max_length=3_000_000)
    barcode: Optional[str] = Field(default=None, max_length=32)


class OfferIn(BaseModel):
    request_id: int
    product_name: str = Field(min_length=1, max_length=200)
    specs: Optional[str] = Field(default=None, max_length=1000)
    price: float = Field(ge=0)
    note: Optional[str] = Field(default=None, max_length=1000)


class NotifyIn(BaseModel):
    notify_email: bool


# ==========================================================================
# Auth endpoints
# ==========================================================================

@app.post("/api/register", tags=["auth"])
def register(data: RegisterIn, background: BackgroundTasks, request: Request,
             conn: sqlite3.Connection = Depends(get_db)):
    # Signups are cheap to make and expensive to clean up: twenty accounts
    # an hour from one address is a script, not a hostel.
    rate_limit("register:%s" % client_ip(request), limit=20, window_seconds=3600)

    if data.role not in ROLES:
        raise HTTPException(400, "role must be 'student' or 'shop'")
    if data.role == "shop" and not (data.shop_name or "").strip():
        raise HTTPException(400, "shop_name is required for shop accounts")
    if data.age is None:
        raise HTTPException(400, "Please enter your age.")
    if data.age < MIN_SIGNUP_AGE:
        raise HTTPException(
            400,
            "You must be at least %d to use this marketplace." % MIN_SIGNUP_AGE)

    email = data.email.lower().strip()
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise HTTPException(409, "An account with that email already exists")

    cur = conn.execute(
        "INSERT INTO users (name, email, password, role, area, shop_name, "
        "phone, age) VALUES (?,?,?,?,?,?,?,?)",
        (data.name.strip(), email, hash_password(data.password), data.role,
         normalize_area(data.area),
         (data.shop_name or "").strip() or None,
         normalize_phone(data.phone) if data.role == "shop" else None,
         data.age),
    )
    user_id = cur.lastrowid
    conn.commit()                       # commit BEFORE queueing the email

    background.add_task(emailer.send_welcome, user_id)
    return {"ok": True, "id": user_id, "role": data.role}


@app.post("/api/login", tags=["auth"])
def login(data: LoginIn, request: Request,
          conn: sqlite3.Connection = Depends(get_db)):
    email = data.email.lower().strip()
    # Two buckets, because one alone has a hole in it. Per-IP stops one
    # machine working through a password list; per-account stops a spread-out
    # attack from many addresses against one known email. Both are generous
    # enough that somebody mistyping their own password never meets them.
    rate_limit("login-ip:%s" % client_ip(request), limit=10, window_seconds=300)
    rate_limit("login-acct:%s" % email, limit=10, window_seconds=900)

    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    # Same message either way, so the response cannot be used to discover
    # which email addresses are registered.
    if row is None or not verify_password(data.password, row["password"]):
        raise HTTPException(401, "Incorrect email or password")
    if row["blocked_at"] is not None:
        raise HTTPException(403, "This account has been suspended")

    token = new_token()
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
        (token, row["id"], stamp(utcnow() + timedelta(days=SESSION_TTL_DAYS))))
    # Housekeeping on the way past: a sign-in is a natural moment to drop
    # this account's dead tokens, and it keeps the table from growing by one
    # row per sign-in forever.
    conn.execute(
        "DELETE FROM sessions WHERE user_id = ? AND expires_at IS NOT NULL "
        "AND expires_at <= ?", (row["id"], stamp(utcnow())))
    conn.commit()
    return {"ok": True, "token": token, "user": public_user(dict(row)),
            "expires_in_days": SESSION_TTL_DAYS}


@app.post("/api/logout", tags=["auth"])
def logout(authorization: Optional[str] = Header(None),
           conn: sqlite3.Connection = Depends(get_db),
           user: dict = Depends(get_current_user)):
    token = authorization.split(" ", 1)[1].strip()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    return {"ok": True}


@app.post("/api/logout-everywhere", tags=["auth"])
def logout_everywhere(conn: sqlite3.Connection = Depends(get_db),
                      user: dict = Depends(get_current_user)):
    """
    Drop every session for this account, this device included.

    The answer to "my phone is gone". Expiry alone would get there in a
    month; this gets there now, and it is the only revocation an ordinary
    user can perform without an administrator.
    """
    cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    conn.commit()
    return {"ok": True, "signed_out": cur.rowcount}


@app.post("/api/password/forgot", tags=["auth"])
def forgot_password(data: ForgotIn, background: BackgroundTasks,
                    request: Request,
                    conn: sqlite3.Connection = Depends(get_db)):
    """
    Start a password reset.

    Always answers the same way, whether or not the address is registered.
    An endpoint that says "no such account" is a tool for working out who
    has an account here, and on a campus that is a list of real people.
    """
    email = data.email.lower().strip()
    rate_limit("forgot-ip:%s" % client_ip(request), limit=5, window_seconds=900)
    rate_limit("forgot-acct:%s" % email, limit=5, window_seconds=3600)

    answer = {"ok": True,
              "message": "If that address has an account, a reset link is on "
                         "its way. Check your inbox and your spam folder."}

    row = conn.execute(
        "SELECT id, blocked_at FROM users WHERE email = ?", (email,)).fetchone()
    if row is None or row["blocked_at"] is not None:
        return answer

    # Any link already outstanding stops working now. Asking twice should
    # leave one live link, not two.
    conn.execute(
        "DELETE FROM password_resets WHERE user_id = ? AND used_at IS NULL",
        (row["id"],))
    token = new_token()
    conn.execute(
        "INSERT INTO password_resets (token_hash, user_id, expires_at) "
        "VALUES (?,?,?)",
        (hash_token(token), row["id"],
         stamp(utcnow() + timedelta(minutes=RESET_TTL_MINUTES))))
    conn.commit()                       # commit BEFORE queueing the email

    background.add_task(emailer.send_password_reset, row["id"], token,
                        reset_link(token), RESET_TTL_MINUTES)
    return answer


@app.post("/api/password/reset", tags=["auth"])
def reset_password(data: ResetIn, request: Request,
                   conn: sqlite3.Connection = Depends(get_db)):
    """Finish a password reset and sign every device out."""
    rate_limit("reset-ip:%s" % client_ip(request), limit=10, window_seconds=900)

    row = conn.execute(
        "SELECT r.token_hash, r.user_id, r.expires_at, r.used_at, u.blocked_at "
        "FROM password_resets r JOIN users u ON u.id = r.user_id "
        "WHERE r.token_hash = ?", (hash_token(data.token.strip()),),
    ).fetchone()

    # One message for every failure: wrong token, used token, expired token.
    # Telling them apart would let somebody test tokens for validity.
    if (row is None or row["used_at"] is not None
            or parse_stamp(row["expires_at"]) <= utcnow()):
        raise HTTPException(
            400, "That reset link is invalid or has expired. Ask for a new one.")
    if row["blocked_at"] is not None:
        raise HTTPException(403, "This account has been suspended")

    conn.execute("UPDATE users SET password = ? WHERE id = ?",
                 (hash_password(data.password), row["user_id"]))
    conn.execute("UPDATE password_resets SET used_at = ? WHERE token_hash = ?",
                 (stamp(utcnow()), row["token_hash"]))
    # Whoever knew the old password is signed out. If the reset happened
    # because the account was taken over, leaving their session alive would
    # make the whole exercise pointless.
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))
    conn.commit()
    return {"ok": True,
            "message": "Password changed. Sign in with your new password."}


@app.get("/api/me", tags=["auth"])
def me(user: dict = Depends(get_current_user)):
    return public_user(user)


@app.post("/api/me/delete", tags=["auth"])
def delete_me(data: DeleteMeIn, conn: sqlite3.Connection = Depends(get_db),
              user: dict = Depends(get_current_user)):
    """
    Delete this account and the personal data attached to it.

    A POST rather than DELETE, which would read better, because a body on a
    DELETE is the kind of thing an intermediary quietly drops -- and the
    body is the password confirmation. Losing it would turn a confirmed
    deletion into a 422, or worse, into an unconfirmed one.

    Withdrawing consent has to actually remove something, or the consent
    was never real. The password is asked for again because a borrowed
    unlocked phone should not be able to erase somebody's shop.

    An owner account is refused here for the same reason it is refused in
    the admin tools: deleting the last administrator locks everyone out of
    the controls, and that is a support problem with no way back.
    """
    if not verify_password(data.password, user["password"]):
        # 403, not 401. The caller IS authenticated -- their token is fine,
        # they mistyped the confirmation. A 401 here would be indistinguishable
        # from an expired session, and a client that signs you out on 401
        # would throw away a perfectly good session over a typo.
        raise HTTPException(403, "That password is not correct")
    if is_admin(user):
        raise HTTPException(
            400, "An owner account cannot be deleted from the app. Remove the "
                 "address from ADMIN_EMAILS first.")

    purge_user(conn, user["id"])
    conn.commit()
    return {"ok": True, "deleted": True}


@app.patch("/api/me", tags=["auth"])
def update_me(data: ProfileIn,
              conn: sqlite3.Connection = Depends(get_db),
              user: dict = Depends(get_current_user)):
    """
    Complete or correct a profile.

    Accounts created before a field existed had no way to gain it, so adding a
    phone number meant registering again under a new address. Only the fields
    actually sent are touched, which keeps a form that edits one thing from
    silently blanking the rest.
    """
    sets, params = [], []

    def put(column, value):
        sets.append("%s = ?" % column)
        params.append(value)

    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(400, "Name cannot be empty")
        put("name", name)
    if data.area is not None:
        put("area", normalize_area(data.area))
    if data.age is not None:
        # The same floor as signup. Correcting a typo is allowed; editing
        # your way under the minimum after the fact is not.
        if data.age < MIN_SIGNUP_AGE:
            raise HTTPException(
                400,
                "Age cannot be below %d on this marketplace." % MIN_SIGNUP_AGE)
        put("age", data.age)
    if data.phone is not None:
        put("phone", normalize_phone(data.phone))
    if data.address is not None:
        put("address", data.address.strip() or None)
    if data.about is not None:
        put("about", data.about.strip() or None)
    if data.clear_location:
        put("lat", None)
        put("lng", None)
    else:
        if data.lat is not None:
            put("lat", data.lat)
        if data.lng is not None:
            put("lng", data.lng)
    if data.avatar is not None:
        if data.avatar.strip():
            blob, media = decode_data_url(data.avatar)
            put("avatar", blob)
            put("avatar_type", media)
        else:
            # An empty string is how a picture is removed, the same way an
            # empty address clears an address.
            put("avatar", None)
            put("avatar_type", None)
    if data.shop_name is not None:
        if user["role"] != "shop":
            raise HTTPException(400, "Only a shop has a shop name")
        shop_name = data.shop_name.strip()
        if not shop_name:
            raise HTTPException(400, "Shop name cannot be empty")
        put("shop_name", shop_name)

    if not sets:
        return public_user(user)

    params.append(user["id"])
    conn.execute("UPDATE users SET " + ", ".join(sets) + " WHERE id = ?", params)
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return public_user(dict(row))


@app.get("/api/categories", tags=["catalog"])
def categories():
    return {"categories": CATEGORIES}


@app.get("/api/areas", tags=["catalog"])
def areas(conn: sqlite3.Connection = Depends(get_db)):
    """
    Areas already in use, with how many shops cover each.

    Clients offer these as suggestions so that everyone in one place spells it
    the same way. Without that, a request and the shops that should answer it
    end up filed under names that never meet. Public on purpose: the signup
    form needs it before anyone has an account.

    Grouped on the lowercase form so "Civil Lines" and "civil lines" count
    once, and the spelling returned is whichever shops use most, since shops
    are the side that has to match for a request to go anywhere.
    """
    rows = conn.execute(
        "SELECT area, "
        "       SUM(CASE WHEN role = 'shop' THEN 1 ELSE 0 END) AS shops, "
        "       COUNT(*) AS people "
        "FROM users "
        "WHERE area IS NOT NULL AND TRIM(area) <> '' AND blocked_at IS NULL "
        "GROUP BY TRIM(LOWER(area)), area "
        "ORDER BY shops DESC, people DESC"
    ).fetchall()

    merged: dict = {}
    for r in rows:
        key = area_key(r["area"])
        entry = merged.get(key)
        if entry is None:
            merged[key] = {"area": r["area"], "shops": r["shops"], "people": r["people"]}
        else:
            # Same place, different capitalisation: keep the spelling that the
            # most shops use and add the counts together.
            if r["shops"] > entry["shops"]:
                entry["area"] = r["area"]
            entry["shops"] += r["shops"]
            entry["people"] += r["people"]

    out = sorted(merged.values(), key=lambda a: (-a["shops"], -a["people"], a["area"]))
    return {"areas": out}


# ==========================================================================
# Student: requests
# ==========================================================================

@app.post("/api/requests", tags=["requests"])
def create_request(data: RequestIn, background: BackgroundTasks,
                   conn: sqlite3.Connection = Depends(get_db),
                   user: dict = Depends(require_student)):
    if data.category not in CATEGORIES:
        raise HTTPException(400, "Unknown category")

    cur = conn.execute(
        "INSERT INTO requests (student_id, title, category, description, "
        "specs, budget) VALUES (?,?,?,?,?,?)",
        (user["id"], data.title.strip(), data.category, data.description,
         data.specs, data.budget),
    )
    request_id = cur.lastrowid

    key = area_key(user["area"])
    if key:
        shops = conn.execute(
            "SELECT id FROM users WHERE role = 'shop' AND blocked_at IS NULL "
            "AND TRIM(LOWER(COALESCE(area,''))) = ? LIMIT 50", (key,)
        ).fetchall()
        for shop in shops:
            notify(
                conn, shop["id"], "request",
                "New request in %s" % (user["area"] or "your area"),
                data.title.strip(), request_id=request_id,
            )
    conn.commit()

    background.add_task(emailer.notify_new_request, request_id)

    # How many shops this actually reached. A request posted into an area with
    # no shops in it looks identical to one nobody has answered yet, and the
    # student waits on an empty screen with nothing to act on. Returning the
    # count lets the client say which of the two just happened.
    return {
        "ok": True,
        "id": request_id,
        "area": canonical_area(conn, user["area"]),
        "shops_notified": count_shops_in_area(conn, user["area"]),
    }


@app.get("/api/requests/mine", tags=["requests"])
def my_requests(conn: sqlite3.Connection = Depends(get_db),
                user: dict = Depends(require_student)):
    rows = conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM offers o WHERE o.request_id = r.id) "
        "AS offer_count, (SELECT MIN(price) FROM offers o WHERE o.request_id "
        "= r.id) AS best_price FROM requests r WHERE r.student_id = ? "
        "ORDER BY r.id DESC", (user["id"],)
    ).fetchall()
    return {"requests": [dict(r) for r in rows]}


@app.get("/api/requests/open", tags=["requests"])
def open_requests(conn: sqlite3.Connection = Depends(get_db),
                  user: dict = Depends(require_shop),
                  my_area_only: bool = Query(False)):
    sql = (
        "SELECT r.*, u.name AS student_name, u.area AS student_area, "
        "(SELECT COUNT(*) FROM offers o WHERE o.request_id = r.id) AS offer_count, "
        "(SELECT 1 FROM offers o WHERE o.request_id = r.id AND o.shop_id = ?) "
        "AS i_offered "
        "FROM requests r JOIN users u ON u.id = r.student_id "
        "WHERE r.status = 'open'"
    )
    params: List = [user["id"]]
    if my_area_only and user["area"]:
        sql += " AND TRIM(LOWER(COALESCE(u.area,''))) = ?"
        params.append(user["area"].strip().lower())
    sql += " ORDER BY r.id DESC"

    rows = conn.execute(sql, params).fetchall()
    return {"requests": [dict(r) for r in rows]}


@app.get("/api/requests/{request_id}/offers", tags=["requests"])
def request_offers(request_id: int,
                   conn: sqlite3.Connection = Depends(get_db),
                   user: dict = Depends(get_current_user)):
    """The comparison table: offers sorted cheapest first, best deal flagged."""
    req = conn.execute("SELECT * FROM requests WHERE id = ?",
                       (request_id,)).fetchone()
    if req is None:
        raise HTTPException(404, "Request not found")

    # Contact details ride along with the offers. A student who has just been
    # shown the cheapest price needs a way to act on it, and sending them back
    # to a separate lookup for a phone number is where that stalls.
    rows = conn.execute(
        "SELECT o.*, u.id AS shop_user_id, u.shop_name, u.name AS shop_contact, "
        "       u.area, u.phone, u.email AS shop_email, "
        "       u.address, u.lat, u.lng "
        "FROM offers o JOIN users u ON u.id = o.shop_id "
        "WHERE o.request_id = ? ORDER BY o.price ASC, o.id ASC",
        (request_id,)
    ).fetchall()

    best = float(rows[0]["price"]) if rows else None
    offers = []
    for r in rows:
        price = float(r["price"])
        offers.append({
            "id": r["id"],
            "shop_id": r["shop_user_id"],
            "shop_name": r["shop_name"] or r["shop_contact"],
            "shop_contact": r["shop_contact"],
            "phone": r["phone"],
            "email": r["shop_email"],
            "address": r["address"],
            "lat": r["lat"],
            "lng": r["lng"],
            "area": r["area"],
            "product_name": r["product_name"],
            "specs": r["specs"],
            "price": price,
            "note": r["note"],
            "created_at": r["created_at"],
            "is_best": price <= best + 1e-9,
            "price_diff_vs_best": round(price - best, 2),
        })

    return {
        "request": {
            "id": req["id"], "title": req["title"], "category": req["category"],
            "specs": req["specs"], "budget": req["budget"],
            "status": req["status"],
        },
        "best_price": best,
        "offer_count": len(offers),
        "offers": offers,
    }


# ==========================================================================
# Shop: products and offers
# ==========================================================================

@app.post("/api/products", tags=["catalog"])
def add_product(data: ProductIn, conn: sqlite3.Connection = Depends(get_db),
                user: dict = Depends(require_shop)):
    if data.category not in CATEGORIES:
        raise HTTPException(400, "Unknown category")
    blob, media = decode_data_url(data.image)
    code = clean_barcode(data.barcode)
    cur = conn.execute(
        "INSERT INTO products (shop_id, name, category, description, specs, "
        "price, stock, barcode, image, image_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user["id"], data.name.strip(), data.category, data.description,
         data.specs, data.price, data.stock, code, blob, media),
    )
    remember_barcode(conn, code, data.name, data.category, data.specs)
    conn.commit()
    return {"ok": True, "id": cur.lastrowid, "has_image": blob is not None}


@app.get("/api/products/mine", tags=["catalog"])
def my_products(conn: sqlite3.Connection = Depends(get_db),
                user: dict = Depends(require_shop)):
    rows = conn.execute(
        "SELECT id, shop_id, name, category, description, specs, price, stock, "
        "       created_at, (image IS NOT NULL) AS has_image "
        "FROM products WHERE shop_id = ? ORDER BY id DESC", (user["id"],)
    ).fetchall()
    return {"products": [dict(r) for r in rows]}


@app.get("/api/products", tags=["catalog"])
def browse_products(q: Optional[str] = None, category: Optional[str] = None,
                    area: Optional[str] = None, limit: int = Query(100, le=500),
                    conn: sqlite3.Connection = Depends(get_db)):
    """Public product browse. No auth: students compare before signing up."""
    sql = ("SELECT p.id, p.shop_id, p.name, p.category, p.description, p.specs, "
           "       p.price, p.stock, p.created_at, "
           "       (p.image IS NOT NULL) AS has_image, "
           "       u.shop_name, u.name AS shop_contact, u.area "
           "FROM products p JOIN users u ON u.id = p.shop_id "
           "WHERE u.blocked_at IS NULL")
    params: List = []
    if q:
        sql += " AND (p.name LIKE ? OR p.description LIKE ? OR p.specs LIKE ?)"
        params += ["%%%s%%" % q] * 3
    if category:
        sql += " AND p.category = ?"
        params.append(category)
    if area:
        sql += " AND TRIM(LOWER(COALESCE(u.area,''))) = ?"
        params.append(area.strip().lower())
    sql += " ORDER BY p.price ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return {"products": [dict(r) for r in rows]}


@app.patch("/api/products/{product_id}", tags=["catalog"])
def edit_product(product_id: int, data: ProductEdit,
                 conn: sqlite3.Connection = Depends(get_db),
                 user: dict = Depends(require_shop)):
    """Change a listing. Only the shop that owns it may."""
    row = conn.execute("SELECT shop_id FROM products WHERE id = ?",
                       (product_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such listing")
    if row["shop_id"] != user["id"]:
        raise HTTPException(403, "That listing belongs to another shop")

    sets, params = [], []

    def put(column, value):
        sets.append("%s = ?" % column)
        params.append(value)

    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(400, "Name cannot be empty")
        put("name", name)
    if data.category is not None:
        if data.category not in CATEGORIES:
            raise HTTPException(400, "Unknown category")
        put("category", data.category)
    if data.description is not None:
        put("description", data.description.strip() or None)
    if data.specs is not None:
        put("specs", data.specs.strip() or None)
    if data.price is not None:
        put("price", data.price)
    if data.stock is not None:
        put("stock", data.stock)
    if data.image is not None:
        if data.image.strip():
            blob, media = decode_data_url(data.image)
            put("image", blob)
            put("image_type", media)
        else:
            put("image", None)
            put("image_type", None)

    if data.barcode is not None:
        put("barcode", clean_barcode(data.barcode))

    if sets:
        params.append(product_id)
        conn.execute("UPDATE products SET " + ", ".join(sets) + " WHERE id = ?", params)
        # A corrected name is the better name, so the shared table learns it.
        fixed = conn.execute(
            "SELECT barcode, name, category, specs FROM products WHERE id = ?",
            (product_id,)
        ).fetchone()
        if fixed is not None:
            remember_barcode(conn, fixed["barcode"], fixed["name"],
                             fixed["category"], fixed["specs"])
        conn.commit()

    out = conn.execute(
        "SELECT id, shop_id, name, category, description, specs, price, stock, "
        "       created_at, (image IS NOT NULL) AS has_image "
        "FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    return dict(out)


@app.delete("/api/products/{product_id}", tags=["catalog"])
def delete_product(product_id: int,
                   conn: sqlite3.Connection = Depends(get_db),
                   user: dict = Depends(require_shop)):
    """
    Withdraw a listing.

    Removed outright rather than hidden behind a flag: a listing is a price
    on a shelf, and nothing else in the database points at one, so there is
    no history to preserve by keeping it. Offers are separate rows.
    """
    row = conn.execute("SELECT shop_id FROM products WHERE id = ?",
                       (product_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such listing")
    if row["shop_id"] != user["id"]:
        raise HTTPException(403, "That listing belongs to another shop")
    conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
    conn.commit()
    return {"ok": True, "id": product_id}


# ==========================================================================
# Owner
# ==========================================================================
#
# Everything here answers 404 to anyone who is not an owner, so the area does
# not announce itself to someone probing for it.


@app.get("/api/admin/users", tags=["admin"])
def admin_users(q: Optional[str] = None, role: Optional[str] = None,
                conn: sqlite3.Connection = Depends(get_db),
                owner: dict = Depends(require_admin)):
    """Every account, blocked ones included -- they are the point."""
    sql = (
        "SELECT u.id, u.name, u.email, u.role, u.area, u.shop_name, u.phone, "
        "       u.age, u.blocked_at, u.created_at, "
        "       (SELECT COUNT(*) FROM products p WHERE p.shop_id = u.id) AS listings, "
        "       (SELECT COUNT(*) FROM offers o WHERE o.shop_id = u.id) AS offers, "
        "       (SELECT COUNT(*) FROM requests r WHERE r.student_id = u.id) AS requests "
        "FROM users u WHERE 1=1"
    )
    params: List = []
    if role in ROLES:
        sql += " AND u.role = ?"
        params.append(role)
    if q:
        sql += " AND (u.name LIKE ? OR u.email LIKE ? OR u.shop_name LIKE ?)"
        params += ["%%%s%%" % q] * 3
    sql += " ORDER BY u.id DESC LIMIT 200"

    rows = conn.execute(sql, params).fetchall()
    return {
        "users": [{
            **dict(r),
            "blocked": r["blocked_at"] is not None,
            # Computed here rather than stored, so it cannot go stale and
            # cannot disagree with the age beside it. The marketplace now
            # admits 14-year-olds, which makes "is this account a child"
            # a question an owner has to be able to answer.
            "minor": r["age"] is not None and r["age"] < 18,
            "is_admin": (r["email"] or "").lower() in ADMIN_EMAILS,
        } for r in rows]
    }


@app.post("/api/admin/users/{user_id}/block", tags=["admin"])
def admin_block(user_id: int, conn: sqlite3.Connection = Depends(get_db),
                owner: dict = Depends(require_admin)):
    row = conn.execute("SELECT id, email FROM users WHERE id = ?",
                       (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such account")
    if (row["email"] or "").lower() in ADMIN_EMAILS:
        # Locking the owners out of their own marketplace would need a
        # database edit to undo, and there is no reason to allow it.
        raise HTTPException(400, "An owner account cannot be suspended")

    conn.execute("UPDATE users SET blocked_at = CURRENT_TIMESTAMP WHERE id = ?",
                 (user_id,))
    # Their sessions go with them; otherwise an open tab keeps working.
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    return {"ok": True, "id": user_id, "blocked": True}


@app.post("/api/admin/users/{user_id}/unblock", tags=["admin"])
def admin_unblock(user_id: int, conn: sqlite3.Connection = Depends(get_db),
                  owner: dict = Depends(require_admin)):
    row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such account")
    conn.execute("UPDATE users SET blocked_at = NULL WHERE id = ?", (user_id,))
    conn.commit()
    return {"ok": True, "id": user_id, "blocked": False}


@app.delete("/api/admin/users/{user_id}", tags=["admin"])
def admin_delete(user_id: int, conn: sqlite3.Connection = Depends(get_db),
                 owner: dict = Depends(require_admin)):
    """Remove an account and everything that belonged to it."""
    row = conn.execute("SELECT id, email FROM users WHERE id = ?",
                       (user_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such account")
    if (row["email"] or "").lower() in ADMIN_EMAILS:
        raise HTTPException(400, "An owner account cannot be deleted here")

    purge_user(conn, user_id)
    conn.commit()
    return {"ok": True, "id": user_id}


@app.post("/api/admin/shops", tags=["admin"])
def admin_add_shop(data: ShopIn, conn: sqlite3.Connection = Depends(get_db),
                   owner: dict = Depends(require_admin)):
    """
    Register a shop on its behalf.

    A shopkeeper who cannot work the signup form can still be put on the
    marketplace by somebody sitting with them, which is how this gets its
    first dozen shops.
    """
    email = data.email.lower().strip()
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise HTTPException(409, "An account with that email already exists")

    cur = conn.execute(
        "INSERT INTO users (name, email, password, role, area, shop_name, phone) "
        "VALUES (?,?,?,'shop',?,?,?)",
        (data.name.strip(), email, hash_password(data.password),
         normalize_area(data.area), data.shop_name.strip(),
         normalize_phone(data.phone)),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


# ==========================================================================
# Barcodes
# ==========================================================================


def clean_barcode(code: Optional[str]) -> Optional[str]:
    """Digits only. Scanners and people both add spaces and dashes."""
    if not code:
        return None
    digits = "".join(ch for ch in code if ch.isdigit())
    # EAN-8 through to a long GTIN. Anything outside that is not a product
    # barcode and should not become a row.
    return digits if 8 <= len(digits) <= 14 else None


def lookup_open_food_facts(code: str):
    """
    Ask Open Food Facts what a barcode is.

    Only consulted when the marketplace has not seen the code before, and the
    answer is written into our own table so it is asked at most once ever.
    Failure is silent and expected: the service is for groceries, most of
    what these shops sell is not, and a shopkeeper waiting on a scan should
    never be held up by somebody else's server.
    """
    import json as _json
    import urllib.request

    url = "https://world.openfoodfacts.org/api/v0/product/%s.json" % code
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "HyperLocalStudentMarketplace/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    if data.get("status") != 1:
        return None
    product = data.get("product") or {}
    name = (product.get("product_name")
            or product.get("generic_name")
            or "").strip()
    if not name:
        return None
    brand = (product.get("brands") or "").split(",")[0].strip()
    quantity = (product.get("quantity") or "").strip()
    if brand and brand.lower() not in name.lower():
        name = "%s %s" % (brand, name)
    return {"name": name[:200], "specs": quantity[:1000] or None}


@app.get("/api/barcode/{code}", tags=["catalog"])
def barcode_lookup(code: str, conn: sqlite3.Connection = Depends(get_db),
                   user: dict = Depends(require_shop)):
    """
    What is this barcode?

    Our own table first, because a name a real shopkeeper typed beats a
    database entry, and because it costs nothing. Open Food Facts only if we
    have never seen the code, and whatever it says is kept so the question is
    asked once for the whole marketplace rather than once per shop.
    """
    clean = clean_barcode(code)
    if not clean:
        raise HTTPException(400, "That is not a product barcode")

    row = conn.execute(
        "SELECT code, name, category, specs, source, seen FROM barcodes WHERE code = ?",
        (clean,)
    ).fetchone()
    if row is not None:
        return {"found": True, **dict(row)}

    found = lookup_open_food_facts(clean)
    if found is None:
        return {"found": False, "code": clean}

    conn.execute(
        "INSERT INTO barcodes (code, name, specs, source, seen) "
        "VALUES (?,?,?,'openfoodfacts',0) "
        "ON CONFLICT(code) DO NOTHING",
        (clean, found["name"], found.get("specs")),
    )
    conn.commit()
    return {
        "found": True, "code": clean, "name": found["name"],
        "category": None, "specs": found.get("specs"),
        "source": "openfoodfacts", "seen": 0,
    }


def remember_barcode(conn: sqlite3.Connection, code: Optional[str],
                     name: str, category: Optional[str],
                     specs: Optional[str]) -> None:
    """
    Teach the marketplace what a shop just called this code.

    A name typed by a shopkeeper who had the thing in their hand is better
    than anything a database will say, so it replaces whatever was there.
    """
    clean = clean_barcode(code)
    if not clean or not name:
        return
    conn.execute(
        "INSERT INTO barcodes (code, name, category, specs, source, seen) "
        "VALUES (?,?,?,?,'shop',1) "
        "ON CONFLICT(code) DO UPDATE SET "
        "  name = excluded.name, category = excluded.category, "
        "  specs = excluded.specs, source = 'shop', "
        "  seen = barcodes.seen + 1, updated_at = CURRENT_TIMESTAMP",
        (clean, name.strip()[:200], category, (specs or "").strip()[:1000] or None),
    )


# ==========================================================================
# Reviews
# ==========================================================================


def review_summary(conn: sqlite3.Connection, shop_id: int) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, AVG(rating) AS avg FROM reviews WHERE shop_id = ?",
        (shop_id,)
    ).fetchone()
    return {
        "count": row["n"] or 0,
        "average": round(row["avg"], 2) if row["avg"] is not None else None,
    }


def has_dealt_with(conn: sqlite3.Connection, student_id: int, shop_id: int) -> bool:
    """
    Whether this shop has actually quoted this student.

    A review is only worth reading if the person writing it was on the other
    side of a real price. Without this a shop can be praised or buried by
    accounts that never spoke to it, which is the failure mode of every
    ratings system that does not check.
    """
    row = conn.execute(
        "SELECT 1 FROM offers o JOIN requests r ON r.id = o.request_id "
        "WHERE o.shop_id = ? AND r.student_id = ? LIMIT 1",
        (shop_id, student_id)
    ).fetchone()
    return row is not None


@app.get("/api/shops/{shop_id}/reviews", tags=["reviews"])
def list_reviews(shop_id: int, limit: int = Query(20, le=50),
                 conn: sqlite3.Connection = Depends(get_db)):
    """Public: a student weighing an offer should not have to sign in first."""
    rows = conn.execute(
        "SELECT v.id, v.rating, v.body, v.created_at, u.name AS student_name "
        "FROM reviews v JOIN users u ON u.id = v.student_id "
        "WHERE v.shop_id = ? ORDER BY v.id DESC LIMIT ?", (shop_id, limit)
    ).fetchall()
    out = review_summary(conn, shop_id)
    out["reviews"] = [dict(r) for r in rows]
    return out


@app.post("/api/shops/{shop_id}/reviews", tags=["reviews"])
def write_review(shop_id: int, data: ReviewIn,
                 conn: sqlite3.Connection = Depends(get_db),
                 user: dict = Depends(require_student)):
    shop = conn.execute(
        "SELECT id, name, shop_name FROM users "
        "WHERE id = ? AND role = 'shop' AND blocked_at IS NULL", (shop_id,)
    ).fetchone()
    if shop is None:
        raise HTTPException(404, "No such shop")

    if not has_dealt_with(conn, user["id"], shop_id):
        raise HTTPException(
            403, "You can review a shop once it has sent you an offer")

    # One per pair: writing again replaces what you said rather than adding
    # to it, so nobody can weight their own opinion by repeating it.
    conn.execute(
        "INSERT INTO reviews (shop_id, student_id, rating, body) VALUES (?,?,?,?) "
        "ON CONFLICT(shop_id, student_id) DO UPDATE SET "
        "  rating = excluded.rating, body = excluded.body, "
        "  created_at = CURRENT_TIMESTAMP",
        (shop_id, user["id"], data.rating, (data.body or "").strip() or None),
    )
    notify(
        conn, shop_id, "review",
        "%s rated you %d/5" % (user["name"], data.rating),
        (data.body or "").strip()[:120] or None,
    )
    conn.commit()
    return {"ok": True, **review_summary(conn, shop_id)}


@app.get("/api/users/{user_id}/avatar", tags=["catalog"])
def user_avatar(user_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """
    Someone's picture. Public, because it appears beside their offers and
    messages to people who may not be signed in, and on its own URL so the
    browser caches it rather than refetching it for every card.
    """
    row = conn.execute(
        "SELECT avatar, avatar_type FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None or row["avatar"] is None:
        raise HTTPException(404, "No picture for that account")
    return Response(
        content=row["avatar"],
        media_type=row["avatar_type"] or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/shops/{shop_id}", tags=["catalog"])
def shop_page(shop_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """
    A shop's public page: who they are, where they are, and everything they
    have listed.

    No account needed. A student comparing offers should be able to look at
    who is quoting them before deciding, and that is exactly the moment they
    are least likely to want a sign-in wall.

    Only the fields a shop has chosen to publish are here; the email address
    is not among them, since it reaches the student through an offer rather
    than through a page anyone can open.
    """
    row = conn.execute(
        "SELECT id, name, shop_name, area, about, address, lat, lng, phone, "
        "       (avatar IS NOT NULL) AS has_avatar, created_at "
        "FROM users WHERE id = ? AND role = 'shop' AND blocked_at IS NULL", (shop_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "No such shop")

    products = conn.execute(
        "SELECT id, name, category, specs, price, stock, "
        "       (image IS NOT NULL) AS has_image "
        "FROM products WHERE shop_id = ? ORDER BY price ASC", (shop_id,)
    ).fetchall()

    stats = conn.execute(
        "SELECT COUNT(*) AS offers, "
        "       SUM(CASE WHEN o.price <= ("
        "           SELECT MIN(x.price) FROM offers x WHERE x.request_id = o.request_id"
        "       ) THEN 1 ELSE 0 END) AS cheapest "
        "FROM offers o WHERE o.shop_id = ?", (shop_id,)
    ).fetchone()

    summary = review_summary(conn, shop_id)

    return {
        "rating": summary["average"],
        "reviews": summary["count"],
        "shop": {
            "id": row["id"],
            "name": row["shop_name"] or row["name"],
            "contact": row["name"],
            "area": row["area"],
            "about": row["about"],
            "address": row["address"],
            "lat": row["lat"],
            "lng": row["lng"],
            "phone": row["phone"],
            "has_avatar": bool(row["has_avatar"]),
            "since": row["created_at"],
        },
        # How often this shop has been the cheapest answer, which is the one
        # thing a student actually wants to know about a stranger's prices.
        "offers": stats["offers"] or 0,
        "cheapest": stats["cheapest"] or 0,
        "products": [dict(p) for p in products],
    }


@app.get("/api/products/{product_id}/image", tags=["catalog"])
def product_image(product_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """
    One product's photograph. Public, like the listing it belongs to, and on
    its own URL so the browser caches it instead of refetching with every
    search.
    """
    row = conn.execute(
        "SELECT image, image_type FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if row is None or row["image"] is None:
        raise HTTPException(404, "No image for that product")
    return Response(
        content=row["image"],
        media_type=row["image_type"] or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/api/offers", tags=["offers"])
def create_offer(data: OfferIn, background: BackgroundTasks,
                 conn: sqlite3.Connection = Depends(get_db),
                 user: dict = Depends(require_shop)):
    req = conn.execute("SELECT * FROM requests WHERE id = ?",
                       (data.request_id,)).fetchone()
    if req is None:
        raise HTTPException(404, "Request not found")
    if req["status"] != "open":
        raise HTTPException(409, "That request is closed")

    cur = conn.execute(
        "INSERT INTO offers (request_id, shop_id, product_name, specs, price, "
        "note) VALUES (?,?,?,?,?,?)",
        (data.request_id, user["id"], data.product_name.strip(), data.specs,
         data.price, data.note),
    )
    offer_id = cur.lastrowid
    notify(
        conn, req["student_id"], "offer",
        "%s offered %s" % (user["shop_name"] or user["name"],
                           ("\u20b9%g" % data.price)),
        data.product_name.strip(),
        request_id=data.request_id, shop_id=user["id"],
    )
    conn.commit()                       # commit BEFORE queueing the email

    background.add_task(emailer.notify_new_offer, offer_id)
    return {"ok": True, "id": offer_id}


@app.get("/api/offers/mine", tags=["offers"])
def my_offers(conn: sqlite3.Connection = Depends(get_db),
              user: dict = Depends(require_shop)):
    rows = conn.execute(
        "SELECT o.*, r.title AS request_title, r.budget, r.status, "
        "(SELECT MIN(price) FROM offers x WHERE x.request_id = o.request_id) "
        "AS best_price FROM offers o JOIN requests r ON r.id = o.request_id "
        "WHERE o.shop_id = ? ORDER BY o.id DESC", (user["id"],)
    ).fetchall()

    offers = []
    for r in rows:
        d = dict(r)
        d["is_best"] = (d["best_price"] is not None
                        and float(d["price"]) <= float(d["best_price"]) + 1e-9)
        offers.append(d)
    return {"offers": offers}


# ==========================================================================
# Notifications
# ==========================================================================


@app.get("/api/notifications", tags=["notifications"])
def list_notifications(limit: int = Query(30, le=100),
                       conn: sqlite3.Connection = Depends(get_db),
                       user: dict = Depends(get_current_user)):
    """My feed, newest first, with how many I have not seen."""
    rows = conn.execute(
        "SELECT id, kind, title, body, request_id, shop_id, read_at, created_at "
        "FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user["id"], limit)
    ).fetchall()
    unread = conn.execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND read_at IS NULL",
        (user["id"],)
    ).fetchone()["n"]
    return {
        "notifications": [{
            "id": r["id"], "kind": r["kind"], "title": r["title"],
            "body": r["body"], "request_id": r["request_id"],
            "shop_id": r["shop_id"], "created_at": r["created_at"],
            "read": r["read_at"] is not None,
        } for r in rows],
        "unread": unread,
    }


@app.post("/api/notifications/read", tags=["notifications"])
def mark_notifications_read(conn: sqlite3.Connection = Depends(get_db),
                            user: dict = Depends(get_current_user)):
    """Mark the lot as seen. Opening the feed is the act of reading it."""
    conn.execute(
        "UPDATE notifications SET read_at = CURRENT_TIMESTAMP "
        "WHERE user_id = ? AND read_at IS NULL", (user["id"],)
    )
    conn.commit()
    return {"ok": True}


# ==========================================================================
# Messages
# ==========================================================================
#
# A thread is a request paired with one shop. The student is whoever owns the
# request, so that pair names both sides without storing either again, and
# two shops answering the same request stay in separate conversations.


def thread_parties(conn: sqlite3.Connection, request_id: int, shop_id: int):
    """The student and shop in a thread, or None if the pair is not real."""
    row = conn.execute(
        "SELECT r.student_id, s.id AS shop_id, s.role "
        "FROM requests r JOIN users s ON s.id = ? "
        "WHERE r.id = ?", (shop_id, request_id)
    ).fetchone()
    if row is None or row["role"] != "shop":
        return None
    return row["student_id"], row["shop_id"]


def may_use_thread(user: dict, student_id: int, shop_id: int) -> bool:
    """Only the two people in a conversation may read or add to it."""
    return user["id"] == student_id or user["id"] == shop_id


@app.get("/api/messages/threads", tags=["messages"])
def my_threads(conn: sqlite3.Connection = Depends(get_db),
               user: dict = Depends(get_current_user)):
    """Every conversation I am part of, most recently active first."""
    if user["role"] == "shop":
        where, params = "m.shop_id = ?", [user["id"]]
    else:
        where, params = "r.student_id = ?", [user["id"]]

    rows = conn.execute(
        "SELECT m.request_id, m.shop_id, r.title, "
        "       s.shop_name, s.name AS shop_contact, "
        "       u.name AS student_name, "
        "       MAX(m.id) AS last_id, COUNT(*) AS total, "
        "       (SELECT body FROM messages x WHERE x.request_id = m.request_id "
        "        AND x.shop_id = m.shop_id ORDER BY x.id DESC LIMIT 1) AS last_body, "
        "       (SELECT created_at FROM messages x WHERE x.request_id = m.request_id "
        "        AND x.shop_id = m.shop_id ORDER BY x.id DESC LIMIT 1) AS last_at, "
        "       COALESCE((SELECT last_read_id FROM thread_reads t "
        "                 WHERE t.request_id = m.request_id AND t.shop_id = m.shop_id "
        "                 AND t.user_id = ?), 0) AS last_read "
        "FROM messages m "
        "JOIN requests r ON r.id = m.request_id "
        "JOIN users s ON s.id = m.shop_id "
        "JOIN users u ON u.id = r.student_id "
        "WHERE " + where + " "
        "GROUP BY m.request_id, m.shop_id "
        "ORDER BY last_id DESC",
        [user["id"]] + params
    ).fetchall()

    threads = []
    for r in rows:
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM messages "
            "WHERE request_id = ? AND shop_id = ? AND id > ? AND sender_id <> ?",
            (r["request_id"], r["shop_id"], r["last_read"], user["id"])
        ).fetchone()["n"]
        threads.append({
            "request_id": r["request_id"],
            "shop_id": r["shop_id"],
            "title": r["title"],
            # Whoever you are talking to, which is the other side of the pair.
            "with_name": (r["student_name"] if user["role"] == "shop"
                          else (r["shop_name"] or r["shop_contact"])),
            "last_body": r["last_body"],
            "last_at": r["last_at"],
            "messages": r["total"],
            "unread": unread,
        })
    return {"threads": threads, "unread_total": sum(t["unread"] for t in threads)}


@app.get("/api/messages/thread", tags=["messages"])
def read_thread(request_id: int, shop_id: int,
                conn: sqlite3.Connection = Depends(get_db),
                user: dict = Depends(get_current_user)):
    """One conversation. Reading it marks it read, for this user only."""
    parties = thread_parties(conn, request_id, shop_id)
    if parties is None:
        raise HTTPException(404, "No such conversation")
    student_id, shop = parties
    if not may_use_thread(user, student_id, shop):
        raise HTTPException(403, "That conversation is not yours")

    rows = conn.execute(
        "SELECT m.id, m.sender_id, m.body, m.created_at, u.name AS sender_name "
        "FROM messages m JOIN users u ON u.id = m.sender_id "
        "WHERE m.request_id = ? AND m.shop_id = ? ORDER BY m.id ASC",
        (request_id, shop_id)
    ).fetchall()

    if rows:
        conn.execute(
            "INSERT INTO thread_reads (request_id, shop_id, user_id, last_read_id) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(request_id, shop_id, user_id) "
            "DO UPDATE SET last_read_id = excluded.last_read_id",
            (request_id, shop_id, user["id"], rows[-1]["id"])
        )
        conn.commit()

    req = conn.execute("SELECT title, budget FROM requests WHERE id = ?",
                       (request_id,)).fetchone()
    return {
        "request_id": request_id,
        "shop_id": shop_id,
        "title": req["title"] if req else None,
        "budget": req["budget"] if req else None,
        "messages": [{
            "id": r["id"],
            "body": r["body"],
            "created_at": r["created_at"],
            "sender_name": r["sender_name"],
            "mine": r["sender_id"] == user["id"],
        } for r in rows],
    }


@app.post("/api/messages", tags=["messages"])
def send_message(data: MessageIn,
                 conn: sqlite3.Connection = Depends(get_db),
                 user: dict = Depends(get_current_user)):
    # A shop is always its own side of the thread, so it need not say which.
    shop_id = user["id"] if user["role"] == "shop" else data.shop_id
    if shop_id is None:
        raise HTTPException(400, "shop_id is required")

    parties = thread_parties(conn, data.request_id, shop_id)
    if parties is None:
        raise HTTPException(404, "No such conversation")
    student_id, shop = parties
    if not may_use_thread(user, student_id, shop):
        raise HTTPException(403, "That conversation is not yours")

    cur = conn.execute(
        "INSERT INTO messages (request_id, shop_id, sender_id, body) VALUES (?,?,?,?)",
        (data.request_id, shop_id, user["id"], data.body.strip()),
    )
    other_id = shop if user["id"] == student_id else student_id
    notify(
        conn, other_id, "message",
        "New message from %s" % (user["shop_name"] or user["name"]),
        data.body.strip()[:120],
        request_id=data.request_id, shop_id=shop_id,
    )
    # Your own message is not something you have to come back and read.
    conn.execute(
        "INSERT INTO thread_reads (request_id, shop_id, user_id, last_read_id) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(request_id, shop_id, user_id) "
        "DO UPDATE SET last_read_id = excluded.last_read_id",
        (data.request_id, shop_id, user["id"], cur.lastrowid)
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid, "shop_id": shop_id}


# ==========================================================================
# Email notification settings
# ==========================================================================

@app.get("/api/notifications/settings", tags=["notifications"])
def get_notify_settings(conn: sqlite3.Connection = Depends(get_db),
                        user: dict = Depends(get_current_user)):
    row = conn.execute("SELECT notify_email FROM users WHERE id = ?",
                       (user["id"],)).fetchone()
    flag = row["notify_email"] if row else 1
    return {"notify_email": bool(flag is None or flag == 1)}


@app.patch("/api/notifications/settings", tags=["notifications"])
def set_notify_settings(data: NotifyIn,
                        conn: sqlite3.Connection = Depends(get_db),
                        user: dict = Depends(get_current_user)):
    conn.execute("UPDATE users SET notify_email = ? WHERE id = ?",
                 (1 if data.notify_email else 0, user["id"]))
    conn.commit()
    return {"ok": True, "notify_email": data.notify_email}


@app.post("/api/notifications/test", tags=["notifications"])
def test_notification(user: dict = Depends(get_current_user)):
    """Verify SMTP settings end to end by emailing yourself."""
    ok = emailer.send_test(user["id"])
    return {"ok": ok, "sent_to": user["email"]}


@app.get("/api/notifications/log", tags=["notifications"])
def notification_log(limit: int = Query(20, le=100),
                     conn: sqlite3.Connection = Depends(get_db),
                     user: dict = Depends(get_current_user)):
    rows = conn.execute(
        "SELECT kind, subject, status, error, created_at FROM email_log "
        "WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user["id"], limit)
    ).fetchall()
    return {"emails": [dict(r) for r in rows]}


# ==========================================================================
# Health
# ==========================================================================

@app.get("/api/health", tags=["meta"])
def health(conn: sqlite3.Connection = Depends(get_db)):
    counts = {}
    for table in ("users", "requests", "offers", "products"):
        counts[table] = conn.execute(
            "SELECT COUNT(*) AS n FROM %s" % table).fetchone()["n"]
    cfg = emailer.get_config()
    return {
        "ok": True,
        "version": app.version,
        "counts": counts,
        "email": {
            "enabled": cfg.enabled,
            "console_only": cfg.console_only,
            "host": cfg.host,
        },
    }
