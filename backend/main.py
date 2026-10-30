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
import secrets
import hashlib
import sqlite3
from typing import Optional, List

from fastapi import FastAPI, Depends, Header, HTTPException, BackgroundTasks, Query
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

CREATE INDEX IF NOT EXISTS idx_offers_request  ON offers(request_id, price);
CREATE INDEX IF NOT EXISTS idx_requests_student ON requests(student_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_products_shop   ON products(shop_id, id DESC);
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


def public_user(user: dict) -> dict:
    """Never let the password hash out of the building."""
    return {
        "id": user["id"], "name": user["name"], "email": user["email"],
        "role": user["role"], "area": user["area"],
        "shop_name": user["shop_name"],
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
        "INSERT INTO users (name, email, password, role, area, shop_name) "
        "VALUES (?,?,?,?,?,?)",
        (data.name.strip(), email, hash_password(data.password), data.role,
         normalize_area(data.area),
         (data.shop_name or "").strip() or None),
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

    rows = conn.execute(
        "SELECT o.*, u.shop_name, u.name AS shop_contact, u.area "
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
            "shop_name": r["shop_name"] or r["shop_contact"],
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
    cur = conn.execute(
        "INSERT INTO products (shop_id, name, category, description, specs, "
        "price, stock) VALUES (?,?,?,?,?,?,?)",
        (user["id"], data.name.strip(), data.category, data.description,
         data.specs, data.price, data.stock),
    )
    conn.commit()
    return {"ok": True, "id": cur.lastrowid}


@app.get("/api/products/mine", tags=["catalog"])
def my_products(conn: sqlite3.Connection = Depends(get_db),
                user: dict = Depends(require_shop)):
    rows = conn.execute("SELECT * FROM products WHERE shop_id = ? "
                        "ORDER BY id DESC", (user["id"],)).fetchall()
    return {"products": [dict(r) for r in rows]}


@app.get("/api/products", tags=["catalog"])
def browse_products(q: Optional[str] = None, category: Optional[str] = None,
                    area: Optional[str] = None, limit: int = Query(100, le=500),
                    conn: sqlite3.Connection = Depends(get_db)):
    """Public product browse. No auth: students compare before signing up."""
    sql = ("SELECT p.*, u.shop_name, u.name AS shop_contact, u.area "
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
