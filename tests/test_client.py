from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from analyst.edgar.client import SecClient, TokenBucket, archive_url, pad_cik

UA = "Test Runner test@example.com"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(tmp_path: Path, handler, **kw) -> SecClient:
    kw.setdefault("limiter", TokenBucket(1e6, sleep=lambda s: None))
    return SecClient(
        UA, tmp_path, transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw
    )


@pytest.mark.parametrize(
    ("raw", "padded"),
    [
        (320193, "0000320193"),
        ("320193", "0000320193"),
        ("CIK0000320193", "0000320193"),
        (1, "0000000001"),
        ("0000789019", "0000789019"),
    ],
)
def test_pad_cik(raw, padded):
    assert pad_cik(raw) == padded


@pytest.mark.parametrize("bad", ["abc", "12345678901", "", "-5"])
def test_pad_cik_rejects_garbage(bad):
    with pytest.raises(ValueError):
        pad_cik(bad)


def test_archive_url_strips_dashes():
    assert archive_url(320193, "0000320193-24-000123", "aapl-20240928.htm") == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"
    )


def test_token_bucket_never_exceeds_rate():
    clock = FakeClock()
    bucket = TokenBucket(rate=8, clock=clock, sleep=clock.sleep)
    times = []
    for _ in range(80):
        bucket.acquire()
        times.append(clock.now)
    assert times[-1] == pytest.approx(79 / 8)  # evenly spaced at 1/8 s
    for t0 in times:  # no 1-second window ever sees more than 8 requests
        assert len([t for t in times if t0 <= t < t0 + 1.0 - 1e-9]) <= 8


def test_token_bucket_burst_is_opt_in():
    clock = FakeClock()
    bucket = TokenBucket(rate=8, capacity=4, clock=clock, sleep=clock.sleep)
    for _ in range(4):
        bucket.acquire()
    assert clock.now == 0
    bucket.acquire()
    assert clock.now == pytest.approx(1 / 8)


def test_user_agent_sent_on_every_request(tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["User-Agent"])
        return httpx.Response(200, json={"ok": True})

    c = make_client(tmp_path, handler)
    c.get_json("https://data.sec.gov/a.json")
    c.get_json("https://data.sec.gov/b.json")
    assert seen == [UA, UA]


@pytest.mark.parametrize("ua", ["", "nobody", "anon@example.com"])
def test_rejects_user_agent_without_name_and_email(tmp_path, ua):
    with pytest.raises(ValueError):
        SecClient(ua, tmp_path)


def test_rejects_rate_over_sec_limit(tmp_path):
    with pytest.raises(ValueError):
        SecClient(UA, tmp_path, rate=11)


def test_cache_hit_makes_no_request(tmp_path):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"n": calls})

    c = make_client(tmp_path, handler)
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    assert c.get_json(url) == {"n": 1}
    assert c.get_json(url) == {"n": 1}
    assert calls == 1
    assert (c.stats.network, c.stats.cache_hits) == (1, 1)
    assert (tmp_path / "data.sec.gov" / "submissions" / "CIK0000320193.json").exists()

    # A fresh client over the same directory (a re-run) also hits the cache.
    c2 = make_client(tmp_path, handler)
    assert c2.get_json(url) == {"n": 1}
    assert c2.stats.network == 0

    # refresh=True bypasses it.
    assert c2.get_json(url, refresh=True) == {"n": 2}


def test_cache_keys_include_query(tmp_path):
    c = make_client(tmp_path, lambda r: httpx.Response(200, text=str(r.url)))
    assert c.cache_path("https://x.gov/a?q=1") != c.cache_path("https://x.gov/a?q=2")


def test_explicit_cache_path(tmp_path):
    c = make_client(tmp_path, lambda r: httpx.Response(200, text="<html/>"))
    target = tmp_path / "filings" / "doc.htm"
    c.get_bytes("https://www.sec.gov/Archives/x.htm", cache_path=target)
    assert target.read_text() == "<html/>"
    c.get_bytes("https://www.sec.gov/Archives/x.htm", cache_path=target)
    assert c.stats.network == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retries_then_succeeds(tmp_path, status):
    responses = [httpx.Response(status), httpx.Response(status), httpx.Response(200, text="ok")]
    sleeps: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    c = SecClient(
        UA,
        tmp_path,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        backoff=0.5,
        limiter=TokenBucket(1000, sleep=lambda s: None),
    )
    assert c.get_bytes("https://data.sec.gov/x") == b"ok"
    assert c.stats.network == 3
    assert sleeps == [0.5, 1.0]  # exponential backoff


def test_honours_retry_after(tmp_path):
    responses = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, text="ok")]
    sleeps: list[float] = []
    c = SecClient(
        UA,
        tmp_path,
        transport=httpx.MockTransport(lambda r: responses.pop(0)),
        sleep=sleeps.append,
        limiter=TokenBucket(1000, sleep=lambda s: None),
    )
    c.get_bytes("https://data.sec.gov/x")
    assert sleeps == [7.0]


def test_gives_up_after_max_retries_and_does_not_cache(tmp_path):
    c = make_client(tmp_path, lambda r: httpx.Response(503), max_retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        c.get_bytes("https://data.sec.gov/x")
    assert c.stats.network == 3
    assert not c.cache_path("https://data.sec.gov/x").exists()


def test_404_is_not_retried(tmp_path):
    c = make_client(tmp_path, lambda r: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        c.get_bytes("https://data.sec.gov/x")
    assert c.stats.network == 1


def test_transport_error_retried(tmp_path):
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text="ok")

    c = make_client(tmp_path, handler)
    assert c.get_bytes("https://data.sec.gov/x") == b"ok"


def test_tickers_map(tmp_path):
    payload = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
    c = make_client(tmp_path, lambda r: httpx.Response(200, json=payload))
    assert c.tickers() == {"AAPL": 320193}
