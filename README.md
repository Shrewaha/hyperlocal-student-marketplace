# HyperLocal Student Marketplace

A two-sided marketplace that flips how students buy local. Instead of walking
shop to shop asking for prices, a student posts **what they need** — specs and
budget — and nearby shops compete by sending offers. The student gets one
comparison table with the best price flagged, and an email the moment a new
offer lands.

Built as a FastAPI JSON backend with a JavaFX desktop client.

---

## Why it works this way

Three decisions shape the whole project:

**Shops come to the student, not the other way around.** A request is broadcast
by email to every shop in the same area, so a small shop with no website can
still compete on price against one that has one.

**The comparison is computed server-side.** The API returns offers sorted
cheapest first with `is_best` and `price_diff_vs_best` already filled in. The
desktop table and the notification email read the same numbers, so they can
never disagree about who is cheapest.

**Email never blocks the API.** Notifications are queued as FastAPI background
tasks. A dead SMTP server slows nothing down and fails no request — it just
writes a `failed` row to the email log.

---

## Features

- Student and shop accounts, with PBKDF2 password hashing and bearer tokens
- Students post requests with category, specs and budget
- Shops see open requests, filtered to their own area by default
- Shops reply with offers; shops also list products in a public catalog
- Automatic price comparison: sorted by price, best deal flagged, difference
  shown against the cheapest
- Email notifications on registration, on a new request (to local shops), and
  on every new offer (to the student) — with the price compared in the email
  body
- Per-user email opt-out, a test-email endpoint, and a delivery log
- Public product search by keyword, category and area — no account needed

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python, FastAPI | Automatic OpenAPI docs at `/docs`, async-friendly, tiny |
| Database | SQLite | One file, zero setup, enough for a campus-scale app |
| Frontend | Java, JavaFX | Desktop client, single file, no build tool required |
| API | Pure JSON REST | No templates, so a web or Android client can be added later |
| Auth | Bearer tokens in a `sessions` table | Simple to reason about and to revoke |
| Email | stdlib `smtplib` | No third-party dependency, works with any SMTP provider |

---

## Quick start

### 1. Backend

```bash
cd backend
pip install -r ../requirements.txt
cp .env.example .env          # Windows: copy .env.example .env
uvicorn main:app --reload
```

The database is created on first run. Interactive API docs:
<http://localhost:8000/docs>

`.env` ships with `EMAIL_CONSOLE_ONLY=true`, so emails print to your terminal
instead of being sent. Nothing leaves your machine until you change that.

Check it works:

```bash
python test_emailer.py        # 14 checks, no SMTP account needed
curl http://localhost:8000/api/health
```

### 2. Frontend

Needs Java 17+, the [JavaFX SDK](https://openjfx.io), and
[json-20231013.jar](https://repo1.maven.org/maven2/org/json/json/20231013/json-20231013.jar)
(put it in `frontend/lib/`).

In IntelliJ IDEA: add both as libraries under **Project Structure → Libraries**,
then set VM options on the run configuration:

```
--module-path "C:\javafx-sdk-21\lib" --add-modules javafx.controls
```

From the command line:

```bash
cd frontend
javac --module-path /path/to/javafx-sdk-21/lib --add-modules javafx.controls \
      -cp lib/json-20231013.jar MarketplaceApp.java
java  --module-path /path/to/javafx-sdk-21/lib --add-modules javafx.controls \
      -cp .:lib/json-20231013.jar MarketplaceApp
```

On Windows use `;` instead of `:` in the classpath.

If the backend runs somewhere other than localhost, change one line:

```java
static final String BASE_URL = "http://localhost:8000";
```

### 3. Try it

Register a student with area `Civil Lines`, register a shop with the **same**
area, post a request as the student, then send an offer as the shop. The offer
email prints in the backend terminal, and the student's comparison table shows
the best deal in green.

---

## API

All protected routes take `Authorization: Bearer <token>` from `/api/login`.

| Method | Endpoint | Auth | Role | Purpose |
|---|---|---|---|---|
| POST | `/api/register` | No | Any | Create a student or shop account |
| POST | `/api/login` | No | Any | Returns a bearer token |
| POST | `/api/logout` | Yes | Any | Invalidate the current token |
| GET | `/api/me` | Yes | Any | Current user |
| GET | `/api/categories` | No | Any | Category list |
| POST | `/api/requests` | Yes | student | Post a request, emails local shops |
| GET | `/api/requests/mine` | Yes | student | My requests, with offer counts |
| GET | `/api/requests/open` | Yes | shop | Open requests (`?my_area_only=true`) |
| GET | `/api/requests/{id}/offers` | Yes | Any | Comparison table |
| POST | `/api/products` | Yes | shop | Add a product listing |
| GET | `/api/products/mine` | Yes | shop | My listings |
| GET | `/api/products` | No | Any | Search (`?q=`, `?category=`, `?area=`) |
| POST | `/api/offers` | Yes | shop | Submit an offer, emails the student |
| GET | `/api/offers/mine` | Yes | shop | My offers, each marked cheapest or beaten |
| GET | `/api/notifications/settings` | Yes | Any | Is email on for me? |
| PATCH | `/api/notifications/settings` | Yes | Any | Turn email on or off |
| POST | `/api/notifications/test` | Yes | Any | Send myself a test email |
| GET | `/api/notifications/log` | Yes | Any | Delivery log for my address |
| GET | `/api/health` | No | Any | Row counts and email config |

### The comparison response

`GET /api/requests/1/offers`

```json
{
  "request": { "id": 1, "title": "ESP8266 NodeMCU", "budget": 850.0 },
  "best_price": 750.0,
  "offer_count": 3,
  "offers": [
    { "shop_name": "Sharma Electronics", "price": 750.0,
      "is_best": true,  "price_diff_vs_best": 0.0 },
    { "shop_name": "Gupta Lab Supplies", "price": 799.0,
      "is_best": false, "price_diff_vs_best": 49.0 }
  ]
}
```

---

## Email notifications

Configured entirely through `backend/.env`. See
[docs/EMAIL.md](docs/EMAIL.md) for the full setup, Gmail App Password steps and
troubleshooting.

| Event | Goes to | Contains |
|---|---|---|
| Registration | The new user | What to do next, role-specific |
| New request | Every shop in the student's area | Specs, budget, category |
| New offer | The student | Price, specs, and whether it beats the current best |

Shops are matched on area, case- and whitespace-insensitively, capped at
`EMAIL_MAX_FANOUT` (default 50). Every send is recorded in the `email_log`
table as `sent`, `failed`, `skipped`, `console` or `duplicate`, and the same
offer is never emailed twice.

---

## Project structure

```
hyperlocal-student-marketplace/
├── backend/
│   ├── main.py            FastAPI app: all endpoints, auth, SQLite schema
│   ├── emailer.py         Notification module, stdlib SMTP only
│   ├── test_emailer.py    14 checks, no SMTP account required
│   └── .env.example       Copy to .env and edit
├── frontend/
│   ├── MarketplaceApp.java   JavaFX client, single file
│   └── lib/                  put json-20231013.jar here
├── docs/
│   └── EMAIL.md
├── requirements.txt
└── README.md
```

---

## Database

Five tables plus a delivery log. Created automatically on first run.

- `users` — students and shops, `role` constrained to one or the other,
  `area` is the matching key, `notify_email` is the opt-out flag
- `requests` — what a student needs, with `budget` and `status`
- `offers` — a shop's reply to a request
- `products` — a shop's standing catalog
- `sessions` — bearer tokens
- `email_log` — one row per send attempt, with status and error

Passwords are stored as `pbkdf2_sha256$iterations$salt$hash`, with a random
per-user salt and 240,000 iterations, verified in constant time.

---

## Roadmap

- [ ] Token expiry and refresh (sessions currently live until logout)
- [ ] Students can mark a request fulfilled and close it
- [ ] Shop ratings after a completed deal
- [ ] Digest mode: batch offer emails instead of one per offer
- [ ] Deploy the backend to Railway or Render
- [ ] Android or web client against the same API
- [ ] Admin view for users and listings

---

## Author

**Shreyansh Kushwaha** — Electrical Engineering undergraduate, IET,
Dr. Rammanohar Lohia Avadh University, Ayodhya.

- GitHub: [@shrewaha](https://github.com/shrewaha)
- Portfolio: [shrewaha.github.io](https://shrewaha.github.io)
- LinkedIn: [shrewaha](https://linkedin.com/in/shrewaha)

---

## License

MIT — see [LICENSE](LICENSE).
