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
   `X-STAFF-Client: 1` header is allowed for non-staff accounts too, **provided
   the caller is proven by a valid Matrix access token** in the `Authorization:
   Bearer` header. The user id from the token is always used; a differing
   `user_id` parameter is ignored (and logged), so the header cannot be used to
   reach another account's nicknames.
3. Everything else is `403 M_FORBIDDEN`. In particular a non-staff request
   with the header but without a valid Matrix access token (for example a
   legacy staff JWT, or only a `user_id` parameter) is refused.

The header is client-controlled: any client that sends it gets rule 2. The
guarantee is therefore "own data only", not "staff only". The same header
already lifts the member-list restriction (see `staff_member_list.md`).

Tests: `tests/test_staff_dm_names.py`.
