# ExamAssist — Backend API contract

The desktop client talks to a small HTTP+JSON backend for accounts and
answer-token accounting. Base URL is configured in `config.API_BASE_URL`
(default `https://api.examassist.app/v1`, override with the
`EXAMASSIST_API_URL` environment variable).

- All bodies are `application/json; charset=utf-8`.
- Authenticated endpoints require `Authorization: Bearer <auth_token>`.
- Errors use HTTP status ≥ 400 and a JSON body of the shape
  `{"error": "human readable message"}` (also accepted: `{"message": "..."}`).

The client stores `{auth_token, user, tokens}` in
`%LOCALAPPDATA%/AudioDeviceAgent/session.json`.

---

## 1. `POST /auth/register`

Create a new account.

**Request**
```json
{
  "full_name": "Jane Doe",
  "email":     "jane@example.com",
  "password":  "at-least-8-chars"
}
```

**Response `200 OK`**
```json
{
  "auth_token": "eyJhbGciOi...opaque bearer...",
  "user": {
    "id":        "usr_01HZ...",
    "full_name": "Jane Doe",
    "email":     "jane@example.com"
  },
  "tokens": 25
}
```

**Common error responses**
- `400` — validation failed (`"error": "Password must be at least 8 characters."`)
- `409` — email already registered

---

## 2. `POST /auth/login`

Exchange credentials for a session token.

**Request**
```json
{ "email": "jane@example.com", "password": "…" }
```

**Response `200 OK`** — identical shape to `/auth/register`:
```json
{
  "auth_token": "…",
  "user": { "id": "usr_…", "full_name": "Jane Doe", "email": "jane@example.com" },
  "tokens": 17
}
```

**Common errors**
- `401` — `{"error": "Invalid email or password."}`

---

## 3. `GET /tokens/balance` &nbsp;·&nbsp; *auth required*

Ask the server how many answer-tokens the current user has. Called at
launch and then every `TOKEN_REFRESH_SECONDS` (default 10 min) so the
in-exam pre-check can stay purely local.

**Response `200 OK`**
```json
{ "tokens": 17 }
```

**Common errors**
- `401` — session expired; client should force sign-in again.

---

## 4. `POST /tokens/consume` &nbsp;·&nbsp; *auth required*

Server-authoritative debit, called AFTER the answer has already been
shown to the user (so nothing blocks their exam). The client also debits
its cached balance immediately for the pre-check on the next question.

**Request**
```json
{
  "amount": 1,
  "meta": { "reason": "answer", "client_version": "1.0.4.2" }
}
```
`meta` is optional and free-form; safe to ignore server-side.

**Response `200 OK`** — the new balance after the debit:
```json
{ "tokens": 16 }
```

**Common errors**
- `402` — insufficient balance (`{"error": "Not enough tokens."}`).
  On this response the client re-syncs from `/tokens/balance` and shows
  the "Out of tokens" panel.
- `401` — session expired.

---

## Client behavior summary

| When                                      | Client action                                                              |
| ----------------------------------------- | -------------------------------------------------------------------------- |
| App start, no local session               | Show Sign-in dialog; call `/auth/login` or `/auth/register`.               |
| App start, session present                | Skip dialog; kick off `/tokens/balance` in the background.                 |
| Hotkey `Ctrl+Alt+Q` pressed               | Check cached `tokens` locally — instant. If < 1, show "Out of tokens".     |
| Answer returned successfully              | Optimistically `tokens -= 1` locally, then `POST /tokens/consume` async.   |
| Every 10 minutes                          | `GET /tokens/balance` to reconcile the local cache.                        |
| Any `401` on an authenticated call        | Clear the session and re-prompt sign-in on next launch.                    |

Purchasing / billing endpoints are intentionally out of scope for this
milestone.
