"""HTTP client for SEC EDGAR.

Guarantees (each has a test in tests/test_client.py):
- every request carries the configured `User-Agent` (SEC fair-access policy);
- requests go through a token bucket limited to `rate` per second (default 8, SEC allows 10);
- 429 and 5xx responses and transport errors are retried with exponential backoff;
- responses are cached on disk under `data/raw/`, keyed by URL, so re-runs never hit SEC.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{doc}"

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def pad_cik(cik: int | str) -> str:
    """Return the 10-digit zero-padded CIK SEC uses in data.sec.gov URLs."""
    digits = str(cik).strip()
    if digits.upper().startswith("CIK"):
        digits = digits[3:]
    if not digits.isdigit() or len(digits) > 10:
        raise ValueError(f"not a CIK: {cik!r}")
    return digits.zfill(10)


def archive_url(cik: int, accession: str, doc: str) -> str:
    return ARCHIVES_URL.format(cik=int(cik), accession_nodash=accession.replace("-", ""), doc=doc)


class TokenBucket:
    """Thread-safe token bucket refilled at `rate` tokens/second.

    `capacity` (default 1) is the burst size. Any burst > 1 lets a 1-second window see
    capacity + rate - 1 requests, so the default keeps requests evenly spaced and every
    1-second window at or under `rate`.
    """

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity if capacity is not None else 1.0
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                self._sleep((1 - self._tokens) / self.rate)


@dataclass
class ClientStats:
    network: int = 0  # requests actually sent to SEC (including retries)
    cache_hits: int = 0


class SecClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        rate: float = 8.0,
        max_retries: int = 5,
        backoff: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        limiter: TokenBucket | None = None,
    ) -> None:
        if "@" not in user_agent or len(user_agent.split()) < 2:
            raise ValueError(
                "SEC_USER_AGENT must be a name and contact email, e.g. 'Jane Doe jane@example.com'"
            )
        if rate > 10:
            raise ValueError("SEC fair access allows at most 10 requests/second")
        self.cache_dir = cache_dir
        self.max_retries = max_retries
        self.backoff = backoff
        self._sleep = sleep
        self.limiter = limiter or TokenBucket(rate, sleep=sleep)
        self.stats = ClientStats()
        self._http = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )

    def __enter__(self) -> SecClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- cache -------------------------------------------------------------------------
    def cache_path(self, url: str) -> Path:
        """`data/raw/<host>/<path>`; a query string adds a short hash so keys stay unique."""
        parts = urlsplit(url)
        path = parts.path.lstrip("/") or "index"
        if parts.query:
            path += "__" + hashlib.sha256(parts.query.encode()).hexdigest()[:12]
        return self.cache_dir / parts.netloc / path

    # -- requests ----------------------------------------------------------------------
    def get_bytes(self, url: str, cache_path: Path | None = None, refresh: bool = False) -> bytes:
        path = cache_path or self.cache_path(url)
        if not refresh and path.exists():
            self.stats.cache_hits += 1
            return path.read_bytes()
        body = self._fetch(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(body)
        tmp.replace(path)  # atomic: an interrupted run never leaves a half-written cache entry
        return body

    def get_json(self, url: str, refresh: bool = False) -> Any:
        # Decimal, not float: reported values must round-trip exactly into NUMERIC columns.
        return json.loads(self.get_bytes(url, refresh=refresh), parse_float=Decimal)

    def _fetch(self, url: str) -> bytes:
        attempt = 0
        while True:
            self.limiter.acquire()
            self.stats.network += 1
            try:
                resp = self._http.get(url)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise
                log.warning("SEC %s: %s (retry %d)", url, exc, attempt + 1)
                self._sleep(self.backoff * 2**attempt)
                attempt += 1
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                delay = self.backoff * 2**attempt
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                log.warning(
                    "SEC %s: HTTP %d (retry %d in %.1fs)", url, resp.status_code, attempt + 1, delay
                )
                self._sleep(delay)
                attempt += 1
                continue
            resp.raise_for_status()
            return resp.content

    # -- endpoints ---------------------------------------------------------------------
    def tickers(self, refresh: bool = False) -> dict[str, int]:
        """Ticker (upper case) -> CIK."""
        data = self.get_json(TICKERS_URL, refresh=refresh)
        return {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}

    def submissions(self, cik: int, refresh: bool = False) -> Any:
        return self.get_json(SUBMISSIONS_URL.format(cik=pad_cik(cik)), refresh=refresh)

    def companyfacts(self, cik: int, refresh: bool = False) -> Any:
        return self.get_json(COMPANYFACTS_URL.format(cik=pad_cik(cik)), refresh=refresh)
