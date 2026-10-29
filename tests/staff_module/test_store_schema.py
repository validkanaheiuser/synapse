#
# STAFF mod — `StaffStore.ensure_schema` gating.
#
# Regression tests for the 2026-10-30 fix: the upgrade DDL that used to run
# inside every `prime_staff_cache()` call (and therefore every
# `refresh_interval_sec` on every worker) must now run at most once per
# process, and only on the process that runs Synapse's background tasks.
#
# Pure unit tests with a fake DatabasePool: no reactor, no homeserver.
#

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, Callable, List

from synapse.staff_module.store import StaffStore


class _FakeTxn:
    def __init__(self, sink: List[str]) -> None:
        self._sink = sink

    def execute(self, sql: str, *args: Any) -> None:
        self._sink.append(" ".join(sql.split()))


class _FakePool:
    """Records DDL issued through runInteraction and counts reads."""

    def __init__(self) -> None:
        self.sql: List[str] = []
        self.interactions: List[str] = []
        self.selects = 0

    async def runInteraction(self, desc: str, func: Callable, *args: Any, **kwargs: Any) -> Any:
        self.interactions.append(desc)
        return func(_FakeTxn(self.sql), *args, **kwargs)

    async def simple_select_list(self, **kwargs: Any) -> list:
        self.selects += 1
        return []

    @property
    def ddl(self) -> List[str]:
        return [s for s in self.sql if s.startswith(("CREATE", "DROP", "ALTER"))]


def _make_store(run_background_tasks: Any = True) -> "tuple[StaffStore, _FakePool]":
    pool = _FakePool()
    hs = SimpleNamespace(
        get_datastores=lambda: SimpleNamespace(main=SimpleNamespace(db_pool=pool)),
        config=SimpleNamespace(worker=SimpleNamespace(run_background_tasks=run_background_tasks)),
    )
    return StaffStore(hs), pool


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class EnsureSchemaGatingTestCase(unittest.TestCase):
    def test_ddl_runs_once_on_background_process(self) -> None:
        store, pool = _make_store(run_background_tasks=True)

        _run(store.prime_staff_cache())
        first = list(pool.ddl)
        self.assertTrue(first, "first prime on the background process must run the upgrade DDL")
        self.assertTrue(any("DROP INDEX IF EXISTS staff_widget_group_members_u" in s for s in first))
        self.assertEqual(pool.selects, 2)

        _run(store.prime_staff_cache())
        _run(store.prime_staff_cache_tracked())
        _run(store.apply_replication_invalidation(("@x:test",)))
        self.assertEqual(pool.ddl, first, "later cache reloads must not re-issue DDL")
        self.assertEqual(pool.selects, 8, "later reloads still refresh the caches")

    def test_plain_worker_never_issues_ddl(self) -> None:
        store, pool = _make_store(run_background_tasks=False)
        _run(store.prime_staff_cache())
        _run(store.prime_staff_cache())
        self.assertEqual(pool.ddl, [])
        self.assertEqual(pool.interactions, [])
        self.assertEqual(pool.selects, 4, "worker still loads the allowlist and edit caches")

    def test_no_worker_config_defaults_to_running_ddl(self) -> None:
        pool = _FakePool()
        hs = SimpleNamespace(get_datastores=lambda: SimpleNamespace(main=SimpleNamespace(db_pool=pool)))
        store = StaffStore(hs)
        _run(store.prime_staff_cache())
        self.assertTrue(pool.ddl)

    def test_ensure_schema_is_idempotent_and_marks_done(self) -> None:
        store, pool = _make_store(run_background_tasks=True)
        self.assertFalse(store._schema_ensured)
        _run(store.ensure_schema())
        n = len(pool.ddl)
        self.assertTrue(store._schema_ensured)
        _run(store.ensure_schema())
        self.assertEqual(len(pool.ddl), n)
