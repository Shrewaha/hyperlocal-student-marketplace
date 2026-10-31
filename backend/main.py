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
import base64
import secrets
import hashlib
import sqlite3
from typing import Optional, List

from fastapi import FastAPI, Depends, Header, HTTPException, BackgroundTasks, Query
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

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- A conversation is a request paired with one shop. Two shops answering the
-- same request are two separate threads, and the student is whoever owns the
-- request, so the pair alone identifies both sides.
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
                          ("avatar_type", "TEXT")):
            if col not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN %s %s" % (col, decl))
        pcols = [r["name"] for r in conn.execute("PRAGMA table_info(products)")]
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


# ==========================================================================
# Auth dependencies
# ==========================================================================

def get_current_user(authorization: Optional[str] = Header(None),
                     conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ?", (token,)
    ).fetchone()
    if row is None:
        raise HTTPException(401, "Invalid or expired token")
    return dict(row)


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
        "WHERE role = 'shop' AND TRIM(LOWER(COALESCE(area,''))) = ?", (key,)
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
        "has_avatar": user.get("avatar") is not None,
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
def register(data: RegisterIn, background: BackgroundTasks,
             conn: sqlite3.Connection = Depends(get_db)):
    if data.role not in ROLES:
        raise HTTPException(400, "role must be 'student' or 'shop'")
    if data.role == "shop" and not (data.shop_name or "").strip():
        raise HTTPException(400, "shop_name is required for shop accounts")

    email = data.email.lower().strip()
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise HTTPException(409, "An account with that email already exists")

    cur = conn.execute(
        "INSERT INTO users (name, email, password, role, area, shop_name, phone) "
        "VALUES (?,?,?,?,?,?,?)",
        (data.name.strip(), email, hash_password(data.password), data.role,
         normalize_area(data.area),
         (data.shop_name or "").strip() or None,
         normalize_phone(data.phone) if data.role == "shop" else None),
    )
    user_id = cur.lastrowid
    conn.commit()                       # commit BEFORE queueing the email

    background.add_task(emailer.send_welcome, user_id)
    return {"ok": True, "id": user_id, "role": data.role}


@app.post("/api/login", tags=["auth"])
def login(data: LoginIn, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM users WHERE email = ?",
                       (data.email.lower().strip(),)).fetchone()
    # Same message either way, so the response cannot be used to discover
    # which email addresses are registered.
    if row is None or not verify_password(data.password, row["password"]):
        raise HTTPException(401, "Incorrect email or password")

    token = new_token()
    conn.execute("INSERT INTO sessions (token, user_id) VALUES (?,?)",
                 (token, row["id"]))
    conn.commit()
    return {"ok": True, "token": token, "user": public_user(dict(row))}


@app.post("/api/logout", tags=["auth"])
def logout(authorization: Optional[str] = Header(None),
           conn: sqlite3.Connection = Depends(get_db),
           user: dict = Depends(get_current_user)):
    token = authorization.split(" ", 1)[1].strip()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    return {"ok": True}


@app.get("/api/me", tags=["auth"])
def me(user: dict = Depends(get_current_user)):
    return public_user(user)


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
        "WHERE area IS NOT NULL AND TRIM(area) <> '' "
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
            "SELECT id FROM users WHERE role = 'shop' "
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
    cur = conn.execute(
        "INSERT INTO products (shop_id, name, category, description, specs, "
        "price, stock, image, image_type) VALUES (?,?,?,?,?,?,?,?,?)",
        (user["id"], data.name.strip(), data.category, data.description,
         data.specs, data.price, data.stock, blob, media),
    )
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
           "FROM products p JOIN users u ON u.id = p.shop_id WHERE 1=1")
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

    if sets:
        params.append(product_id)
        conn.execute("UPDATE products SET " + ", ".join(sets) + " WHERE id = ?", params)
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
        "FROM users WHERE id = ? AND role = 'shop'", (shop_id,)
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

    return {
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
