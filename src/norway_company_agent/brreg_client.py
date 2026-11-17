"""Polite, cached, retrying client for the Brønnøysund open JSON APIs.

* one in-run cache keyed by URL (a re-request inside the same run is never sent twice);
* a global concurrency cap so the whole batch never has more than N requests in flight;
* exponential backoff with jitter on 429/5xx/network errors, honouring ``Retry-After``;
* deterministic, non-retryable classification of Brreg's "unsupported statement layout" 500s;
* exact raw bytes are hashed (SHA-256) so every claim can point at the response it came from.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

USER_AGENT = "KinpakuSignalpostAgent/1.0 (company research; polite batch client)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(text: str) -> int | float:
    value = Decimal(text)
    return int(value) if value == value.to_integral_value() else float(value)


def parse_json(raw: bytes) -> Any:
    # Keep amounts exact: 2375000.00 -> 2375000 (int), never a float rounding artefact.
    return json.loads(raw, parse_float=_number)


@dataclass
class Response:
    url: str
    status: int  # HTTP status, or 0 for network failure
    body: Any = None
    raw_sha256: str | None = None
    retrieved_at: str | None = None
    elapsed_ms: int = 0
    bytes_received: int = 0
    attempts: int = 0
    error: str | None = None
    retryable: bool = False
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.body is not None


@dataclass
class RequestStats:
    network_requests: int = 0
    cache_hits: int = 0
    bytes_received: int = 0
    latencies_ms: list[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, *, requests: int = 0, cache_hits: int = 0, bytes_received: int = 0, latency_ms: int | None = None) -> None:
        with self.lock:
            self.network_requests += requests
            self.cache_hits += cache_hits
            self.bytes_received += bytes_received
            if latency_ms is not None:
                self.latencies_ms.append(latency_ms)


class BrregClient:
    def __init__(self, *, max_in_flight: int = 4, timeout: float = 20.0, attempts: int = 4, min_interval_s: float = 0.0):
        self._semaphore = threading.BoundedSemaphore(max_in_flight)
        self._cache: dict[str, Response] = {}
        self._cache_lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self.timeout = timeout
        self.attempts = attempts
        self.stats = RequestStats()
        # Throttled lanes (e.g. the annual-account copy year list, ~30 starts/min observed limit).
        self._lane_lock = threading.Lock()
        self._lane_last: dict[str, float] = {}
        self._min_interval_s = min_interval_s
        self._global_last = 0.0
        self._global_lock = threading.Lock()

    # -- throttling -------------------------------------------------------
    def _wait_lane(self, lane: str | None, interval: float) -> None:
        if not lane or interval <= 0:
            return
        with self._lane_lock:
            last = self._lane_last.get(lane, 0.0)
            delay = interval - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
            self._lane_last[lane] = time.monotonic()

    def _wait_global(self) -> None:
        if self._min_interval_s <= 0:
            return
        with self._global_lock:
            delay = self._min_interval_s - (time.monotonic() - self._global_last)
            if delay > 0:
                time.sleep(delay)
            self._global_last = time.monotonic()

    # -- public API -------------------------------------------------------
    def get_json(self, url: str, *, lane: str | None = None, lane_interval_s: float = 0.0, stats: RequestStats | None = None) -> Response:
        with self._cache_lock:
            cached = self._cache.get(url)
            waiter = self._inflight.get(url) if cached is None else None
            if cached is None and waiter is None:
                self._inflight[url] = threading.Event()
        if cached is not None:
            self.stats.add(cache_hits=1)
            if stats:
                stats.add(cache_hits=1)
            return Response(**{**cached.__dict__, "from_cache": True})
        if waiter is not None:
            waiter.wait(self.timeout * self.attempts + 30)
            return self.get_json(url, lane=lane, lane_interval_s=lane_interval_s, stats=stats)
        try:
            response = self._fetch(url, lane=lane, lane_interval_s=lane_interval_s, stats=stats)
            # Only cache final outcomes; transient failures may be retried by a later run.
            with self._cache_lock:
                self._cache[url] = response
            return response
        finally:
            with self._cache_lock:
                event = self._inflight.pop(url, None)
            if event:
                event.set()

    def _fetch(self, url: str, *, lane: str | None, lane_interval_s: float, stats: RequestStats | None) -> Response:
        last = Response(url, 0, error="not attempted", retrieved_at=utc_now())
        for attempt in range(1, self.attempts + 1):
            self._wait_lane(lane, lane_interval_s)
            self._wait_global()
            started = time.monotonic()
            retry_after: float | None = None
            request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
            with self._semaphore:
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as http:
                        raw = http.read()
                        elapsed = int((time.monotonic() - started) * 1000)
                        self._record(stats, len(raw), elapsed)
                        try:
                            body = parse_json(raw)
                        except ValueError:
                            last = Response(url, http.status, raw_sha256=hashlib.sha256(raw).hexdigest(), retrieved_at=utc_now(), elapsed_ms=elapsed, bytes_received=len(raw), attempts=attempt, error="invalid JSON", retryable=True)
                            continue
                        return Response(url, http.status, body, hashlib.sha256(raw).hexdigest(), utc_now(), elapsed, len(raw), attempt)
                except urllib.error.HTTPError as exc:
                    raw = exc.read() or b""
                    elapsed = int((time.monotonic() - started) * 1000)
                    self._record(stats, len(raw), elapsed)
                    message = ""
                    try:
                        message = str((parse_json(raw) or {}).get("message") or "")
                    except Exception:
                        message = raw[:200].decode("utf-8", "replace")
                    sha = hashlib.sha256(raw).hexdigest()
                    if exc.code in (404, 410):
                        return Response(url, exc.code, None, sha, utc_now(), elapsed, len(raw), attempt, error=f"HTTP {exc.code}")
                    if exc.code == 500 and "oppstillingsplan" in message.casefold():
                        # Deterministic: Brreg's open API does not serve this statement layout (banks/insurers).
                        return Response(url, exc.code, None, sha, utc_now(), elapsed, len(raw), attempt, error=f"HTTP 500: {message[:200]}", retryable=False)
                    if exc.code in (400, 401, 403):
                        return Response(url, exc.code, None, sha, utc_now(), elapsed, len(raw), attempt, error=f"HTTP {exc.code}: {message[:200]}", retryable=False)
                    header = exc.headers.get("Retry-After") if exc.headers else None
                    if header and header.strip().isdigit():
                        retry_after = min(60.0, float(header.strip()))
                    last = Response(url, exc.code, None, sha, utc_now(), elapsed, len(raw), attempt, error=f"HTTP {exc.code}: {message[:200]}", retryable=True)
                except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                    elapsed = int((time.monotonic() - started) * 1000)
                    self._record(stats, 0, elapsed)
                    last = Response(url, 0, retrieved_at=utc_now(), elapsed_ms=elapsed, attempts=attempt, error=f"{type(exc).__name__}: {str(exc)[:160]}", retryable=True)
            if attempt < self.attempts:
                backoff = retry_after if retry_after is not None else min(20.0, 0.75 * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
                time.sleep(backoff)
        return last

    def _record(self, stats: RequestStats | None, nbytes: int, elapsed: int) -> None:
        self.stats.add(requests=1, bytes_received=nbytes, latency_ms=elapsed)
        if stats:
            stats.add(requests=1, bytes_received=nbytes, latency_ms=elapsed)
