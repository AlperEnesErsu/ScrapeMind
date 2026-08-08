# ScrapeMind API v1

JSON API under `/api/v1`. Stateless **JWT bearer-token** auth (no session
cookie, CSRF-exempt). Signed HS256 via Authlib.

## Authentication

Two token types share one signing key:

| Token   | Lifetime (default) | Use                                              |
|---------|--------------------|--------------------------------------------------|
| access  | 15 min             | `Authorization: Bearer <token>` on every request |
| refresh | 30 days            | Exchange at `/auth/refresh` for a fresh token pair |

### Revocation

Revocation is split by token type so the hot path stays cheap:

- **Access tokens** carry a `ver` claim mirroring the user's `token_version`.
  The auth guard already loads the user, so the check costs no extra query.
  Bumping `token_version` retires every outstanding token at once — this is
  what a **password change** and a **password reset** do.
- **Refresh tokens** carry a unique `jti` checked against a denylist. That
  lookup only happens on `/auth/refresh` and `/auth/logout`, never on a normal
  API call.

**Refresh tokens rotate on every use**: `/auth/refresh` burns the token you
present and returns a new one. A stolen refresh token is therefore usable only
until the legitimate client next refreshes — whichever copy is used second is
rejected as revoked.

Deactivating or deleting a user is also honoured: every request re-loads the
user and rejects inactive/deleted accounts.

Denylist rows are dropped once the token would have expired anyway, by the
nightly `core.purge_revoked_tokens` task.

### `POST /api/v1/auth/token`

Password grant. Body (JSON or form):

```json
{ "username": "alice", "password": "…", "otp_code": "123456" }
```

`otp_code` is **required only if the account has 2FA enabled** (authenticator
code or a one-shot recovery code). `username` accepts username or email.

`200` →
```json
{
  "token_type": "Bearer",
  "access_token": "…",
  "refresh_token": "…",
  "expires_in": 900
}
```

Failure codes: `422 missing_credentials`, `401 invalid_credentials`,
`401 otp_required`, `401 invalid_otp`. Rate-limited to 10/min.
Brute-force lockout is shared with the web login (5 failures → 15-min lock).

### `POST /api/v1/auth/refresh`

Body: `{ "refresh_token": "…" }` →
`200 { "token_type", "access_token", "refresh_token", "expires_in" }`.

Returns a **new refresh token** — the presented one is revoked (rotation).
Store the new one; reusing the old one returns `401 token_revoked`.

Failure: `422 missing_token`, `401 invalid_token`, `401 token_revoked`,
`401 user_inactive`. Rate-limited to 30/min.

### `POST /api/v1/auth/logout`

Body: `{ "refresh_token": "…" }` → `200 { "revoked": true }`.

Revokes that refresh token. **Idempotent**: an already-revoked, expired, or
unparseable token still returns `{"revoked": true}` — the caller's goal holds
either way, and reporting otherwise would leak token state. Access tokens
issued from it stay valid until they expire (≤15 min); to kill those
immediately, change the password.

Failure: `422 missing_token`. Rate-limited to 30/min.

## Resources

All require a valid access token unless noted.

| Method & path               | Description                                        |
|-----------------------------|----------------------------------------------------|
| `GET /api/v1/health`        | Liveness probe. **No auth.** `{ "status": "ok" }`  |
| `GET /api/v1/me`            | The authenticated user.                            |
| `GET /api/v1/papers`        | Global paper catalog, newest first. Paginated.     |
| `GET /api/v1/papers/<id>`   | A single paper, or `404 not_found`.                |
| `GET /api/v1/me/papers`     | Papers surfaced to the caller (excludes dismissed).|

## Writes

`<id>` below is a **`user_paper_id`** (from `GET /me/papers`), not a paper id.

These are **idempotent by design**: the web UI toggles on click, but an API
client that retries must not flip state back — so each flag is `PUT` (set) /
`DELETE` (unset) rather than a single toggle endpoint.

| Method & path                              | Description                          |
|--------------------------------------------|--------------------------------------|
| `PUT`/`DELETE` `/me/papers/<id>/favorite`   | Star / unstar.                       |
| `PUT`/`DELETE` `/me/papers/<id>/read-later` | Bookmark / un-bookmark.              |
| `PUT`/`DELETE` `/me/papers/<id>/dismissed`  | Hide from feed / restore.            |
| `POST /me/papers/<id>/seen`                 | Stamp `seen_at` (only the first time).|
| `GET /me/papers/<id>/notes`                 | Notes on that paper.                 |
| `POST /me/papers/<id>/notes`                | Create a note → `201`.               |
| `PATCH /notes/<note_id>`                    | Update a note (omitted fields kept). |
| `DELETE /notes/<note_id>`                   | Delete → `{ "deleted": true }`.      |
| `GET /me/feeds`                             | The caller's custom RSS feeds.       |
| `POST /me/feeds`                            | Add one → `201`. Body: `{ "url", "label" }`. |

Flag endpoints return the updated paper: `{ "data": { …, "is_favorite": true } }`.

### Custom feeds

`POST /me/feeds` runs the same validation as the web UI — SSRF guard, the
`MAX_USER_FEEDS` cap, and a live fetch that must actually parse as a feed. Any
of those failing returns `422 validation_error` with the reason in `message`.
Re-posting a URL you already have reactivates that row instead of adding a
second one, and does not consume a cap slot.

Feeds have no update or delete endpoint yet, and **YouTube channel
subscriptions are not exposed through the API at all** — they are web-UI only
(`/settings/profile?tab=ai`).

### Notes

`POST` body: `{ "body": "…", "tag": "soru" }`. `body` is required and must not
be blank (`422 empty_body`). `tag` is optional; allowed values are `deney`,
`soru`, `sonuç`, `okuma` — **an unrecognised tag is dropped, not rejected**,
and the response echoes what was actually stored.

`PATCH` follows PATCH semantics: omit a field to keep its current value.

### Ownership

Acting on another user's paper or note returns **`404 not_found`**, not `403` —
the API never confirms that an id it doesn't own exists.

### Pagination

List endpoints accept `?page=` (default 1) and `?per_page=` (default 20, max
100) and return:

```json
{
  "data": [ … ],
  "pagination": { "page": 1, "per_page": 20, "total": 46, "pages": 3 }
}
```

## Error format

Every error is:

```json
{ "error": { "code": "machine_readable_code", "message": "Human text." } }
```

Common codes: `authorization_required`, `invalid_token`, `token_revoked`,
`user_inactive`, `not_found`, `empty_body`, `method_not_allowed`, `forbidden`,
`server_error`.

## Configuration

| Env var           | Default      | Meaning                                        |
|-------------------|--------------|------------------------------------------------|
| `JWT_SECRET_KEY`  | `SECRET_KEY` | Signing key. Falls back to the session key.    |
| `JWT_ISSUER`      | `scrapemind` | `iss` claim, validated on decode.              |
| `JWT_ACCESS_TTL`  | `900`        | Access token lifetime, seconds.                |
| `JWT_REFRESH_TTL` | `2592000`    | Refresh token lifetime, seconds (30 days).     |

Set `JWT_SECRET_KEY` separately from `SECRET_KEY` to rotate API tokens without
invalidating web sessions.

## Example

```bash
TOKEN=$(curl -s localhost:5050/api/v1/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"secret"}' | jq -r .access_token)

curl -s localhost:5050/api/v1/me -H "Authorization: Bearer $TOKEN"
curl -s "localhost:5050/api/v1/papers?per_page=5" -H "Authorization: Bearer $TOKEN"
```
