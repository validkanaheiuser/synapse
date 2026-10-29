# STAFF member-list visibility

When `staff.enabled` is true, an ordinary account whose effective room power
level is **0**, without a valid STAFF client header, can list only:

- accounts in the server's STAFF allowlist, regardless of their room power;
- room members with effective power **50 or greater**.

The requester is not automatically included in this restricted list. STAFF
requesters, requesters with nonzero room power, and requests carrying a valid
`X-STAFF-Client: 1` header retain the full list. The header exception also
applies to ordinary, non-allowlisted accounts with power 0.
Power calculation uses Matrix's normal rules, including `users_default` and
implicit creator privileges. Header-tagged requests still require normal
authentication and permission to access the room.

Both `GET /rooms/{roomId}/members` and `GET /rooms/{roomId}/joined_members`
apply the policy after the existing room-access checks and before username
masking. Membership query filters still apply. Current permissions are used
even when `/members` requests historical state with `at`.

Responses include `io.element.staff.member_list_restricted`, a boolean that
lets the patched Element Web member-list store distinguish a restricted result
from an ordinary full response. The web client builds restricted rows from
this response, rather than mixing them with members cached by `/sync` or
IndexedDB. Search and the displayed member count use these allowed rows;
third-party invitations are omitted for restricted viewers. If the permission
request fails, the web list stays empty rather than showing cached members.

Deploy both the Synapse changes and the Element Web changes for this behavior
in the web UI. Room state and `/sync` are preserved for room processing and
encryption. This feature restricts member-list enumeration and the patched web
UI; it does not remove participant identities from messages or modify member
lists assembled from state by other Matrix clients.

Tests:

- `python -m unittest tests.test_staff_member_list` (policy)
- `python -m twisted.trial tests.rest.client.test_staff_member_list` (REST API)
- From `element-web/apps/web`: `pnpm exec jest --runInBand test/unit-tests/stores/MemberListStore-test.ts test/unit-tests/components/views/rooms/memberlist/MemberListView-test.tsx`
