"""Shared fixtures.

DB tests run against the Postgres in DATABASE_URL (docker compose locally, a service in
CI), each test session inside its own throwaway schema so real data is never touched.
If Postgres is unreachable the DB tests are skipped locally but fail in CI.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from analyst.config import get_settings
from analyst.db.models import Base
from analyst.edgar.client import SecClient, TokenBucket

FIXTURES = Path(__file__).parent / "fixtures"
TEST_UA = "analyst-agent-tests tests@example.com"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    url = get_settings().database_url
    admin = create_engine(url)
    try:
        with admin.connect() as conn:
            conn.execute(text("select 1"))
    except Exception as exc:  # pragma: no cover - depends on the environment
        admin.dispose()
        if os.environ.get("CI"):
            raise
        pytest.skip(
            f"Postgres not reachable at DATABASE_URL ({type(exc).__name__}); "
            "run `docker compose up -d db`"
        )
    schema = f"test_{uuid.uuid4().hex[:10]}"
    with admin.begin() as conn:
        conn.execute(text(f'create schema "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'drop schema "{schema}" cascade'))
        admin.dispose()


@pytest.fixture
def db(pg_engine: Engine) -> Iterator[Engine]:
    """A clean schema for each test."""
    with pg_engine.begin() as conn:
        conn.execute(text("truncate facts, filings, companies restart identity cascade"))
    yield pg_engine


@pytest.fixture
def session_factory(db: Engine) -> Callable[[], AbstractContextManager[Session]]:
    factory = sessionmaker(bind=db, expire_on_commit=False)

    @contextmanager
    def scope() -> Iterator[Session]:
        s = factory()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()

    return scope


class FakeSec:
    """Serves recorded fixtures for SEC URLs and counts requests. No network."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.routes = {
            "https://www.sec.gov/files/company_tickers.json": "company_tickers.json",
        }
        for t, cik in (("aapl", "0000320193"), ("msft", "0000789019")):
            self.routes[f"https://data.sec.gov/submissions/CIK{cik}.json"] = f"{t}_submissions.json"
            self.routes[f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"] = (
                f"{t}_companyfacts.json"
            )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        assert request.headers["User-Agent"] == TEST_UA
        if url in self.routes:
            return httpx.Response(200, content=(FIXTURES / self.routes[url]).read_bytes())
        if url.startswith("https://www.sec.gov/Archives/edgar/data/"):
            return httpx.Response(200, text=f"<html><body>{url}</body></html>")
        return httpx.Response(404)


@pytest.fixture
def fake_sec() -> FakeSec:
    return FakeSec()


@pytest.fixture
def sec_client(tmp_path: Path, fake_sec: FakeSec) -> Callable[[], SecClient]:
    """Factory: each call is a fresh client (like a new CLI run) over the same disk cache."""

    def make() -> SecClient:
        return SecClient(
            TEST_UA,
            tmp_path / "raw",
            transport=httpx.MockTransport(fake_sec),
            sleep=lambda s: None,
            limiter=TokenBucket(1e6, sleep=lambda s: None),
        )

    return make
