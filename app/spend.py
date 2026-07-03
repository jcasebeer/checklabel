"""Per-network spend accounting for open (auth-less) deployments.

The demo deployment leaves the endpoints reachable without login, so the abuse
case isn't request volume — it's someone scripting the endpoints and draining
the Anthropic budget. Instead of rate limiting requests, we cap estimated
model *dollars* per client network over a rolling window.

Bucketing: an IPv4 client is its full address (all 32 bits). An IPv6 client is
bucketed by the top 32 bits of its address — a typical ISP-level allocation —
so rotating addresses within a /64 or /56 does not reset the meter.

The ledger is in-memory, matching the app's no-persistence design: a restart
forgets history, which errs briefly in visitors' favor and is fine for a cap
whose job is stopping sustained abuse.
"""
from __future__ import annotations

import ipaddress
import threading
import time

from . import config

# Conservative per-request cost model for pre-flight checks: a prepared panel
# is ~1,600 image tokens at the size cap, plus schema/prompt overhead and a
# structured-output response.
_EST_TOKENS_PER_IMAGE = 1600
_EST_TOKENS_OVERHEAD = 400
_EST_TOKENS_OUT = 300

WINDOW_SECONDS = 24 * 60 * 60


def bucket_for_ip(ip: str) -> str:
    """Ledger bucket for a client address: IPv4 /32, IPv6 top 32 bits."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip  # e.g. the TestClient's "testclient" — treat as its own bucket
    return str(ipaddress.ip_network((addr, 32), strict=False))


def usage_usd(usage: dict | None) -> float:
    """Actual cost of a completed call from the API's reported token usage."""
    if not usage:
        return 0.0
    return (usage.get("input_tokens", 0) * config.USD_PER_MTOK_IN
            + usage.get("output_tokens", 0) * config.USD_PER_MTOK_OUT) / 1e6


def estimate_usd(n_images: int, batch_pricing: bool = False) -> float:
    """Pre-flight estimate for a label check with n_images panels."""
    tokens_in = n_images * _EST_TOKENS_PER_IMAGE + _EST_TOKENS_OVERHEAD
    usd = (tokens_in * config.USD_PER_MTOK_IN
           + _EST_TOKENS_OUT * config.USD_PER_MTOK_OUT) / 1e6
    return usd / 2 if batch_pricing else usd


class SpendLedger:
    """Rolling-window USD ledger keyed by network bucket."""

    def __init__(self, window_seconds: float = WINDOW_SECONDS):
        self.window = window_seconds
        self._events: dict[str, list[tuple[float, float]]] = {}
        self._lock = threading.Lock()

    def _prune(self, bucket: str, now: float) -> None:
        cutoff = now - self.window
        events = self._events.get(bucket, [])
        events = [(t, usd) for t, usd in events if t >= cutoff]
        if events:
            self._events[bucket] = events
        else:
            self._events.pop(bucket, None)

    def spent(self, bucket: str, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            self._prune(bucket, now)
            return sum(usd for _, usd in self._events.get(bucket, []))

    def charge(self, bucket: str, usd: float, now: float | None = None) -> None:
        if usd <= 0:
            return
        now = time.time() if now is None else now
        with self._lock:
            self._prune(bucket, now)
            self._events.setdefault(bucket, []).append((now, usd))

    def would_exceed(self, bucket: str, usd_estimate: float) -> bool:
        """Whether taking on usd_estimate would push the bucket over the cap."""
        cap = config.SPEND_CAP_PER_IP_USD
        if cap <= 0:
            return False
        return self.spent(bucket) + usd_estimate > cap
