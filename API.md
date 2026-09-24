# ExamAssist desktop — AssessDesk API contract

Base URL: `config.API_BASE_URL` (default `https://api.scholardesk.pro/api/v1/assessdesk`,
override with `EXAMASSIST_API_URL`). JSON everywhere. Errors:
`{"success": false, "error": {"code", "message", "details"}}`.

All timestamps are UTC ISO-8601 (`...Z`). Countdowns use the `*_in_seconds`
fields so a wrong local clock can't change what the user sees.

Accounts are created on https://assessdesk.scholardesk.pro (email, name,
password). Customers, experts and admins sign in with their existing login.
Each new AssessDesk account gets **5 free questions** (valid 7 days).

## Sign-in (password, then 6-digit emailed code)
1. `POST /auth/login` `{email, password}` → `{otp_session_id}`
2. `POST /auth/verify-otp` `{otp_session_id, otp, client_device_id, device_name, client_version}`
   → `{access_token, access_expires_in, refresh_token, device_id, user, balance}`
3. `POST /auth/resend-otp` `{email}` → `{otp_session_id}`

Access token: 15 min, sent as `Authorization: Bearer`. Refresh token: 30 days,
**one-time use** (rotated by `POST /auth/refresh {refresh_token}`), kept in the
Windows Credential Manager. `POST /auth/logout` revokes this device. Max 3
signed-in devices per account; admins can sign a user out everywhere.

## Balance
`GET /me/balance` →
```json
{ "questions_remaining": 17, "tokens": 17, "in_grace": false,
  "next_expiry": { "questions_remaining": 5, "expires_at": "…Z", "usable_until": "…Z",
                   "expires_in_seconds": 86000, "usable_for_seconds": 96800, "status": "active" },
  "grants": [ … ], "warning": { "level": "low", "message": "…" },
  "low_balance_thresholds": [10, 3, 0], "server_time": "…Z" }
```
`warning.level`: `none | low (≤10) | critical (≤3) | expiring (≤72h / ≤24h) | grace | empty`.

Expiry: a batch expires exactly `validity_days × 24h` after purchase. It then
stays usable for a **3-hour grace window** (user is emailed), then is forfeited.
Questions are always spent from the batch that expires first.

## Consume
`POST /me/consume` `{amount: 1, idempotency_key: "<32 hex>", meta?}` → balance.
Retrying the same key never charges twice. `402 INSUFFICIENT_CREDITS` when empty.

## Client behaviour
| When | Action |
| --- | --- |
| No device session | Sign-in dialog (password → code). No sign-up in the app. |
| Launch / every 10 min | `GET /me/balance` (backoff with jitter on errors), flush queued debits first |
| Hotkey | Local cached balance check — instant |
| Answer shown | Queue debit locally (durable), `POST /me/consume` in background |
| Offline | Debits stay queued with their keys, sent on reconnect |
| Warning level changes | Shown once per threshold crossing; never over an answer being read |
| Refresh rejected | Session cleared, user asked to restart and sign in |
