"""Shared fixtures.

DB tests run against the Postgres in DATABASE_URL (docker compose locally, a service in
CI), each test session inside its own throwaway schema so real data is never touched.
If Postgres is unreachable the DB tests are skipped locally but fail in CI.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

import analyst.db.session
from analyst.config import get_settings
from analyst.db.models import Base
from analyst.edgar.client import SecClient, TokenBucket

FIXTURES = Path(__file__).parent / "fixtures"
TEST_UA = "analyst-agent-tests tests@example.com"
TEST_SCHEMAS: dict[Engine, str] = {}  # engine -> its throwaway schema


@pytest.fixture(autouse=True)
def _no_internet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not reach the network: only loopback (the test Postgres) is allowed."""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
            raise RuntimeError(f"test tried to open a network connection to {host!r}")
        real_connect(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def _no_default_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code under test must get the test engine/session injected. Falling back to the
    default engine would read and write the real dev DB's public tables."""

    def refuse(url: str | None = None) -> Engine:
        raise RuntimeError("test used the default DB engine; inject a session or engine")

    monkeypatch.setattr(analyst.db.session, "get_engine", refuse)


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
        conn.execute(text("create extension if not exists vector"))  # lives in public
        conn.execute(text(f'create schema "{schema}"'))
    # public stays on the path for the pgvector type and operators; tables go in schema.
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    TEST_SCHEMAS[engine] = schema
    try:
        # checkfirst=False: with public on the path, the "does it exist?" check would see
        # the real public tables and skip creating ours, and tests would then write to
        # (and truncate!) the real data. Unqualified CREATE goes to the first schema.
        Base.metadata.create_all(engine, checkfirst=False)
        with engine.connect() as conn:
            assert_isolated(conn, schema)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'drop schema "{schema}" cascade'))
        admin.dispose()


def assert_isolated(conn: Connection, schema: str) -> None:
    """Every model table must resolve to the throwaway schema, never to public."""
    for table in Base.metadata.sorted_tables:
        found = conn.execute(
            text(
                "select n.nspname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where c.oid = to_regclass(:t)"
            ),
            {"t": table.name},
        ).scalar()
        if found != schema:
            raise RuntimeError(f"test table {table.name!r} resolves to schema {found!r}")


@pytest.fixture
def db(pg_engine: Engine) -> Iterator[Engine]:
    """A clean schema for each test."""
    schema = TEST_SCHEMAS[pg_engine]
    tables = ", ".join(f'"{schema}"."{t.name}"' for t in Base.metadata.sorted_tables)
    with pg_engine.begin() as conn:
        assert_isolated(conn, schema)
        conn.execute(text(f"truncate {tables} restart identity cascade"))
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
