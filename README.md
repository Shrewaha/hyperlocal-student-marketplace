# HyperLocal Student Marketplace

[![tests](https://github.com/Shrewaha/hyperlocal-student-marketplace/actions/workflows/tests.yml/badge.svg)](https://github.com/Shrewaha/hyperlocal-student-marketplace/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/Shrewaha/hyperlocal-student-marketplace?display_name=tag&sort=semver)](https://github.com/Shrewaha/hyperlocal-student-marketplace/releases/latest)
[![license](https://img.shields.io/github/license/Shrewaha/hyperlocal-student-marketplace)](LICENSE)

A two-sided marketplace that flips how students buy local. Instead of walking
shop to shop asking for prices, a student posts **what they need** — specs and
budget — and nearby shops compete by sending offers. The student gets one
comparison table with the best price flagged, and an email the moment a new
offer lands.

Built as a FastAPI JSON backend with a JavaFX desktop client.

---

## Download

Grab the client from the [latest release](https://github.com/Shrewaha/hyperlocal-student-marketplace/releases/latest).

| You have | Take | How to run it |
|---|---|---|
| No Java installed | `HyperLocalMarketplace-<version>-<your-platform>.zip` or `.tar.gz` | Unzip, open the `HyperLocalMarketplace` app inside. A Java runtime is bundled. |
| Java 17 or newer | `MarketplaceApp.jar` | `java -jar MarketplaceApp.jar` |

Platform names are `windows-x64`, `macos-arm64` (M1 and later), `macos-x64`
(Intel) and `linux-x64`. `SHA256SUMS.txt` in the same release verifies what you
downloaded.

**macOS will refuse to open it the first time.** The bundle is not signed with
an Apple Developer certificate, so Gatekeeper blocks it. Right-click the app and
choose **Open**, then confirm — once only. Or from the terminal:

```bash
xattr -dr com.apple.quarantine /path/to/HyperLocalMarketplace.app
```

### Pointing it at a backend

The client needs the API running somewhere. By default it looks at
`http://localhost:8000`, so start the backend first (see
[Quick start](#quick-start) below). To aim it elsewhere:

```bash
MARKETPLACE_API=https://api.example.com java -jar MarketplaceApp.jar
# or
java -Dmarketplace.api=https://api.example.com -jar MarketplaceApp.jar
```

The bundled app reads the same `MARKETPLACE_API` environment variable.

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

The quickest path, if you have Maven, is to let it fetch JavaFX and org.json
for you:

```bash
mvn -f frontend/pom.xml clean package
java -jar frontend/target/MarketplaceApp.jar
```

That produces the same jar the releases ship. To work on the client by hand
instead, the original setup is unchanged and needs no build tool.

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
│   ├── Launcher.java         entry point for the packaged build only
│   ├── pom.xml               optional Maven build, produces the release jar
│   └── lib/                  put json-20231013.jar here
├── .github/workflows/
│   ├── tests.yml          backend checks on every push
│   └── release.yml        builds and publishes downloads on a v* tag
├── docs/
│   └── EMAIL.md
├── CHANGELOG.md
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

## Cutting a release

```bash
# 1. bump <version> in frontend/pom.xml and move the Unreleased block in
#    CHANGELOG.md under the new number
# 2. commit, then:
git tag v1.0.0
git push origin v1.0.0
```

The `release` workflow builds the jar once, wraps it in a self-contained app on
four platforms, checksums everything and opens a **draft** release with the
files attached. Read it over, edit the notes, then publish. Nothing goes public
until you click the button.

---

## Roadmap

- [ ] Token expiry and refresh (sessions currently live until logout)
- [ ] Students can mark a request fulfilled and close it
- [ ] Shop ratings after a completed deal
- [ ] Digest mode: batch offer emails instead of one per offer
- [ ] Deploy the backend to Railway or Render, so the download works without
      running a server yourself
- [ ] Signed macOS and Windows builds, so Gatekeeper and SmartScreen stop
      warning about them
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
