#
# STAFF mod - Prometheus metrics (S14, Agent I).
#
# Exposes a Counter + Histogram pair that mirrors Synapse's own
# `synapse.http.request_metrics` style (see request_metrics.py:36-58 for the
# pattern we copied: prometheus_client.Counter / Histogram with a
# `SERVER_NAME_LABEL` baked into labelnames).
#
# Call `record_request(server_name, endpoint, status, duration_seconds)` from
# the request-handling path (Agent H wires this into the shared rest_base /
# servlet wrapper).  See `record_request` docstring for the contract.
#

import logging
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from prometheus_client import Counter, Histogram

from synapse.metrics import SERVER_NAME_LABEL

logger = logging.getLogger(__name__)


# Total number of STAFF API requests, split by endpoint name and HTTP status.
# `endpoint` is the short slug Agent H derives from PATTERNS (e.g.
# "settings_get_all"); `status` is the HTTP code as a string (so 2xx/4xx/5xx
# can be aggregated easily in Prometheus).
staff_request_total = Counter(
    "staff_request_total",
    "Total STAFF API requests by endpoint and HTTP status",
    labelnames=["endpoint", "status", SERVER_NAME_LABEL],
)


# Latency histogram, in seconds, per endpoint.
staff_request_duration_seconds = Histogram(
    "staff_request_duration_seconds",
    "STAFF API request latency in seconds, per endpoint",
    labelnames=["endpoint", SERVER_NAME_LABEL],
)


def record_request(
    server_name: str,
    endpoint: str,
    status: int,
    duration_seconds: float,
) -> None:
    """Record one STAFF API request's outcome.

    Designed to be called by Agent H's request wrapper around every staff
    servlet.  Cheap (one Counter.inc + one Histogram.observe) and never
    raises — failures are logged at debug to avoid feedback loops where the
    metrics path could fault the request itself.

    Args:
        server_name: `hs.hostname` (matches the SERVER_NAME_LABEL convention
            used throughout synapse.metrics).
        endpoint: short slug for the endpoint (e.g. "wipe_room",
            "settings_get_all").  Should be stable across requests so the
            cardinality stays bounded.
        status: HTTP status code that was returned.
        duration_seconds: wall-clock duration of the request, in seconds.
    """
    try:
        staff_request_total.labels(
            endpoint=endpoint,
            status=str(int(status)),
            **{SERVER_NAME_LABEL: server_name},
        ).inc()
        staff_request_duration_seconds.labels(
            endpoint=endpoint,
            **{SERVER_NAME_LABEL: server_name},
        ).observe(max(0.0, float(duration_seconds)))
    except Exception:  # pragma: no cover - defensive
        logger.debug("STAFF metrics: failed to record request", exc_info=True)


@contextmanager
def time_request(
    server_name: str, endpoint: str
) -> Iterator["_RequestTimerHandle"]:
    """Context manager that times a request and emits the metric on exit.

    Optional convenience for callers (Agent H) that don't already have a
    wrapper.  Usage:

        with time_request(hs.hostname, "wipe_room") as t:
            ...
            t.status = 200
    """
    handle = _RequestTimerHandle()
    start = time.monotonic()
    try:
        yield handle
    finally:
        duration = time.monotonic() - start
        record_request(
            server_name=server_name,
            endpoint=endpoint,
            status=handle.status or 0,
            duration_seconds=duration,
        )


class _RequestTimerHandle:
    """Mutable handle returned by `time_request`; let callers set status."""

    __slots__ = ("status",)

    def __init__(self) -> None:
        self.status: Optional[int] = None
