# STAFF: custom DM / room nicknames (`/api/dm-names`)

Endpoints (also mirrored under `/_synapse/staff/v1/dm-names`):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/dm-names?user_id=@me:domain` | list the caller's nicknames (`{room_id: {original_name, custom_name}}`) |
| `POST` | `/api/dm-names` | body `{user_id, room_id, original_name?, custom_name}` upsert |
| `DELETE` | `/api/dm-names/<room_id>?user_id=@me:domain` | remove one |

Nicknames are stored per account; an account can only ever read or change
its own rows.

## Who may call it

1. **Staff accounts** (in the staff allowlist): allowed as before, whether the
   caller is identified by a Matrix access token or only by the `user_id`
   parameter.
2. **Any account on the STAFF client**: a request carrying the
   `X-STAFF-Client: 1` header is allowed for non-staff accounts too. When a
   valid Matrix access token is presented in `Authorization: Bearer`, the user
   id from the token is used and a differing `user_id` parameter is ignored
   (and logged). Without a usable token the `user_id` parameter is trusted,
   exactly as on the legacy staff path.
3. Everything else is `403 M_FORBIDDEN`.

Trust model (operator decision, 2026-10-29): the header is client-controlled
and the parameter is unverified, so anyone who sends the header can read or
change the nicknames stored under any `user_id`. The data is only display
nicknames. The same header already lifts the member-list restriction (see
`staff_member_list.md`).

Tests: `tests/test_staff_dm_names.py`.
