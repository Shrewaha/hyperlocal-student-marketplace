# Email notifications

Already wired into `backend/main.py` — nothing to integrate. This page is
about configuring it, testing it, and fixing it when mail does not arrive.

---

## What gets sent

| Event | Recipient | Trigger |
|---|---|---|
| `welcome` | The new user | `POST /api/register` |
| `new_request` | Every shop in the student's area | `POST /api/requests` |
| `new_offer` | The student who posted the request | `POST /api/offers` |

The offer email is the one that matters. It carries the shop, product, specs
and price, then a verdict line computed at send time: either *this is the
lowest offer so far* or *this is Rs. X more than the best offer so far*, plus
a within-budget or over-budget line. It uses the same figures as
`GET /api/requests/{id}/offers`, so the email and the comparison table can
never disagree.

---

## Configuration

Everything lives in `backend/.env`, copied from `.env.example`. It is
gitignored — never commit it.

| Variable | Default | Notes |
|---|---|---|
| `EMAIL_ENABLED` | `true` | Master switch |
| `EMAIL_CONSOLE_ONLY` | `true` | Print to terminal instead of sending |
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | |
| `SMTP_STARTTLS` | `true` | For port 587 |
| `SMTP_SSL` | `false` | Set true (and STARTTLS false) for port 465 |
| `SMTP_USER` | — | Your address |
| `SMTP_PASSWORD` | — | **App Password**, not your account password |
| `SMTP_FROM` | falls back to `SMTP_USER` | |
| `SMTP_FROM_NAME` | `Student Marketplace` | Display name |
| `EMAIL_MAX_FANOUT` | `50` | Cap on shops emailed per request |
| `MARKETPLACE_DB` | `marketplace.db` | Shared with `main.py` |

### Gmail

1. Turn on 2-Step Verification on the account.
2. Create an App Password at <https://myaccount.google.com/apppasswords>.
3. Put those 16 characters in `SMTP_PASSWORD`.

A normal Gmail password is rejected with a 535 error. There is no way around
this — Google removed plain-password SMTP access.

---

## Testing

**Without an SMTP account.** Leave `EMAIL_CONSOLE_ONLY=true`:

```bash
cd backend
python test_emailer.py
```

14 checks covering the area fan-out, best-price logic, deduplication,
opt-out, HTML escaping, Hindi and rupee input, and misconfiguration. It builds
its own `test_marketplace.db` and never touches your real data. Exits non-zero
on failure, so it doubles as a regression check.

**Through the running app.** Still in console mode, register a student and a
shop in the same area, post a request, submit two offers. Three email blocks
print in the terminal; the second offer should state how much more it costs
than the first.

**Live.** Set `EMAIL_CONSOLE_ONLY=false`, fill in SMTP, then verify the
connection on its own:

```bash
python emailer.py your.address@gmail.com
```

Then from the running API: `POST /api/notifications/test` with your token.

---

## Delivery log

Every attempt writes a row to `email_log`:

| Status | Meaning |
|---|---|
| `sent` | The SMTP server accepted it |
| `console` | Printed to the terminal (console mode) |
| `failed` | SMTP refused or the connection broke — see the `error` column |
| `skipped` | Email disabled, or no valid recipient |
| `duplicate` | Already sent for that event and address |

Read your own rows through `GET /api/notifications/log`, or query directly:

```sql
SELECT created_at, to_email, kind, status, error
FROM email_log ORDER BY id DESC LIMIT 20;
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `[email] skipped ... EMAIL_ENABLED is off` | `.env` missing, or not in the folder you ran `uvicorn` from. It is read relative to the working directory. |
| `SMTPAuthenticationError (535)` | Account password instead of an App Password, or 2-Step Verification is off. |
| Hangs, then `SMTPServerDisconnected` or a timeout | Port blocked, or the wrong port/TLS pair. 587 needs `SMTP_STARTTLS=true` and `SMTP_SSL=false`; 465 needs the opposite. College and hostel networks often block 587 — try a phone hotspot to confirm. |
| Shops in the area got nothing | Area is matched on the **student's** `area` against each shop's `area`. Case and surrounding spaces are ignored, spelling is not: "Civil Lines" and "civil-lines" do not match. |
| Nothing arrives, no error | Check spam. Then `GET /api/notifications/log` — `sent` means the server took it and the problem is downstream. |
| `status: duplicate` | Deduplication by event and address. One email per offer per recipient, so a client retry cannot double-send. |
| `UnicodeEncodeError` on Windows | Shouldn't happen: console output falls back to ASCII when the terminal cannot encode it, while the email itself still goes out as UTF-8. If it appears elsewhere, run `set PYTHONUTF8=1` before `uvicorn`. |

---

## Design notes

**Background tasks, always after commit.** Each notification is queued with
`background.add_task(...)` *after* `conn.commit()`. Queueing before the commit
would race: the task opens its own connection and could miss the row.

**Failures are contained.** `send_email()` never raises. A broken SMTP server
produces a logged `failed` row, not a failed API request.

**Sends are serialized.** A module-level lock means a burst of offers opens one
SMTP connection at a time instead of twenty in parallel, which is what gets an
address throttled.

**Connections are closed.** Every database handle the module opens is closed in
a `finally`. A plain `with sqlite3.connect(...)` commits but leaves the handle
open, which on Windows eventually produces `database is locked`.

---

## Known limits

- **No retries.** A failed send is logged and dropped. Adding a `pending`
  status and a retry loop is the natural next step.
- **No digesting.** Ten offers in a minute means ten emails. Batching per
  request on a 15-minute timer would fix it.
- **Fan-out is serial inside the task.** Fine for 50 shops; past a few hundred
  this wants a real queue.
