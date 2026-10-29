"""Focused tests for the DM-names authorisation policy (resolve_dm_names_user)."""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from synapse.api.errors import AuthError, Codes, SynapseError
from synapse.staff_module.rest_dm_names import resolve_dm_names_user


class FakeRequest:
    def __init__(self, headers: dict | None = None):
        self._headers = {}
        for k, v in (headers or {}).items():
            key = k if isinstance(k, bytes) else k.encode("ascii")
            self._headers[key.lower()] = v if isinstance(v, bytes) else v.encode("ascii")

    def getHeader(self, name):
        key = name if isinstance(name, bytes) else name.encode("ascii")
        return self._headers.get(key.lower())


class DmNamesAuthTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.staff = "@staff:test"
        self.alice = "@alice:test"
        self.bob = "@bob:test"
        self.staff_ids = {self.staff}
        self.store = SimpleNamespace(is_staff_user=self.staff_ids.__contains__)
        self.tokens = {"tok-alice": self.alice, "tok-staff": self.staff}

        async def get_user_by_req(request, allow_guest=False):
            raw = request.getHeader(b"Authorization") or b""
            token = raw.split(b" ", 1)[-1].decode()
            if token not in self.tokens:
                raise AuthError(401, "Unrecognised access token", Codes.UNKNOWN_TOKEN)
            user = self.tokens[token]
            return SimpleNamespace(user=SimpleNamespace(to_string=lambda: user))

        self.auth = SimpleNamespace(get_user_by_req=AsyncMock(side_effect=get_user_by_req))
        self.hs = SimpleNamespace(get_auth=lambda: self.auth)

    async def resolve(self, headers=None, param=None):
        return await resolve_dm_names_user(self.hs, self.store, FakeRequest(headers), param)

    async def assert_forbidden(self, headers=None, param=None):
        with self.assertRaises(SynapseError) as cm:
            await self.resolve(headers, param)
        self.assertEqual(cm.exception.code, 403)
        self.assertEqual(cm.exception.errcode, Codes.FORBIDDEN)

    # --- unchanged staff behaviour -------------------------------------------------

    async def test_staff_param_only_still_allowed(self) -> None:
        self.assertEqual(await self.resolve(param=self.staff), self.staff)

    async def test_staff_with_matrix_token_allowed_without_header(self) -> None:
        self.assertEqual(
            await self.resolve({"Authorization": "Bearer tok-staff"}), self.staff
        )

    async def test_staff_with_unverifiable_bearer_falls_back_to_param(self) -> None:
        self.assertEqual(
            await self.resolve({"Authorization": "Bearer a.b.c"}, param=self.staff), self.staff
        )

    # --- non-staff ----------------------------------------------------------------

    async def test_non_staff_without_header_is_forbidden_even_with_token(self) -> None:
        await self.assert_forbidden({"Authorization": "Bearer tok-alice"})
        await self.assert_forbidden(param=self.alice)

    async def test_non_staff_with_header_and_matrix_token_allowed(self) -> None:
        for header in ("X-STAFF-Client", "x-staff-client"):
            for value in ("1", "true"):
                with self.subTest(header=header, value=value):
                    self.assertEqual(
                        await self.resolve({"Authorization": "Bearer tok-alice", header: value}),
                        self.alice,
                    )

    async def test_non_staff_header_token_wins_over_spoofed_param(self) -> None:
        self.assertEqual(
            await self.resolve(
                {"Authorization": "Bearer tok-alice", "X-STAFF-Client": "1"}, param=self.bob
            ),
            self.alice,
        )

    async def test_non_staff_header_without_token_trusts_param(self) -> None:
        # param only
        self.assertEqual(await self.resolve({"X-STAFF-Client": "1"}, param=self.alice), self.alice)
        # unknown Matrix token -> falls back to the parameter
        self.assertEqual(
            await self.resolve(
                {"X-STAFF-Client": "1", "Authorization": "Bearer tok-unknown"}, param=self.alice
            ),
            self.alice,
        )
        # legacy staff JWT shape (not a Matrix token) -> falls back to the parameter
        self.assertEqual(
            await self.resolve(
                {"X-STAFF-Client": "1", "Authorization": "Bearer a.b.c"}, param=self.alice
            ),
            self.alice,
        )

    async def test_non_staff_header_with_token_ignores_param(self) -> None:
        self.assertEqual(
            await self.resolve(
                {"X-STAFF-Client": "1", "Authorization": "Bearer tok-alice"}, param=self.bob
            ),
            self.alice,
        )

    async def test_non_staff_invalid_header_value_keeps_forbidden(self) -> None:
        await self.assert_forbidden({"Authorization": "Bearer tok-alice", "X-STAFF-Client": "0"})
        await self.assert_forbidden({"Authorization": "Bearer tok-alice", "X-STAFF-Client": "yes"})

    # --- parameter validation -----------------------------------------------------

    async def test_missing_and_invalid_user_id(self) -> None:
        with self.assertRaises(SynapseError) as cm:
            await self.resolve({"X-STAFF-Client": "1"})
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(cm.exception.errcode, Codes.MISSING_PARAM)
        with self.assertRaises(SynapseError) as cm:
            await self.resolve({"X-STAFF-Client": "1"}, param="not-a-user-id")
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(cm.exception.errcode, Codes.INVALID_PARAM)
