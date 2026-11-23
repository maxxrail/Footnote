# CLAUDE.md — instructions for Claude Code

You are building **analyst-agent**: an AI financial analyst that answers questions about
public companies from SEC filings, with every number and claim cited back to its source,
and that produces banker-style deliverables (comps tables in Excel, one-page tearsheets).

The project is built in **6 parts**. Each part has a spec in `docs/parts/PART-N.md`.

## Commands

```
uv sync --extra dev                  # install
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -rs                    # no network, no LLM; live tests: -m live
uv run alembic upgrade head          # after pulling or changing models
uv run alembic revision --autogenerate -m "..."   # after changing a model
uv run alembic check                 # models and migrations agree
docker compose up -d db              # local Postgres + pgvector
```

Slash commands in `.claude/commands/`: `/build-part N`, `/check`, `/status`.
Subagent in `.claude/agents/`: `part-reviewer` (run before every PR).

Every change must pass ruff, ruff format, mypy and pytest before a commit.

## Workflow for every part

1. Read `PROGRESS.md` to see which part is current. Read that part's spec in
   `docs/parts/PART-N.md` **in full** before writing code.
2. `git switch main && git pull`, then `git switch -c part-N-<topic>`.
3. List what needs Maxx (accounts, money, secrets, Docker) and say so at the start,
   not at the end.
4. Build **only** the current part. Small commits; tests come with the code, not after.
   Commit messages like `part-2: hybrid retrieval over filing sections`.
5. For every guarantee the part claims (cited numbers, refusals, idempotent ingest…),
   break the code on purpose once and confirm a test fails (mutation check), then
   restore. Mention it in the PR.
6. One ADR per real decision in `docs/adr/NNNN-title.md` (copy `0000-template.md`), with
   the numbers that decided it. Small calls go in `PROGRESS.md` under "Decisions".
7. A part is done only when **every** acceptance check in its spec passes. Paste the
   results into `PROGRESS.md`, update `CHANGELOG.md` (version `0.N.0`), the README
   status, and bump the version in `pyproject.toml` and `analyst/__init__.py`.
8. Run the `part-reviewer` subagent and fix what it finds.
9. `git push -u origin part-N-<topic>` and open a PR with `gh pr create`. The body says
   what was built, what was verified and how, and what was NOT verified.
10. Stop and report. Maxx reviews and merges; then tag `v0.N.0` on main.

If a spec is ambiguous, pick the simplest option that satisfies the acceptance checks,
record the decision, and keep going.

## Rules that must never break

- Never push to main, never force-push, never rewrite pushed history.
- Never commit `.env` or any key or token. Never print secrets in output.

## Hard rules (these are what make the project credible to a finance-AI team)

- **The LLM never does arithmetic.** Every computed number (growth, margins, multiples,
  medians) comes from a deterministic Python function in `analyst/metrics/` with unit tests.
- **Every factual claim is cited.** Answers carry citations to a filing (accession number,
  section, character span) or an XBRL fact (concept, period, unit, filing). An answer with
  an uncited number is a bug.
- **Refuse rather than guess.** If the data is not in the corpus, the agent says so.
- **No secrets in git.** Keys live in `.env` (gitignored); `.env.example` lists the names.
- **SEC fair access:** every EDGAR request sends `User-Agent` from `SEC_USER_AGENT`
  (e.g. `Maxx Railton maxx@example.com`) and stays under 10 requests/second. Cache raw
  responses on disk under `data/raw/` so re-runs do not hit SEC again.
- Tests must not call the network or the LLM. Use recorded fixtures in `tests/fixtures/`
  and a fake LLM client. Live tests go behind `@pytest.mark.live` (skipped by default).

## Stack

- Python 3.12, managed with `uv`. Lint/format with `ruff`. Types checked with `mypy` (lenient).
- FastAPI + Pydantic v2 for the API. SQLAlchemy 2 + Alembic for the DB.
- Postgres 16 + pgvector via `docker compose` (SQLite is **not** used — keep one DB).
- LLM: Anthropic SDK behind a small `LLMClient` protocol in `analyst/llm/` so a fake can
  be injected in tests. Model comes from `ANTHROPIC_MODEL` in `.env`.
- Embeddings: behind an `Embedder` protocol. Default: a local `sentence-transformers`
  model (`BAAI/bge-small-en-v1.5`) so it costs nothing; swappable.
- Frontend (Part 6): React + Vite + TypeScript in `web/`.
- The user is on **Windows** with VS Code, GitHub Desktop and Docker Desktop. Prefer
  commands that work in PowerShell; put multi-step commands in `Makefile`-free
  `scripts/*.py` or `uv run` tasks rather than bash-only scripts.

## Layout (grows part by part)

```
analyst/            # Python package
  config.py         # settings from .env (pydantic-settings)
  db/               # models, session, migrations
  edgar/            # SEC client, ingestion            (Part 1)
  retrieval/        # chunking, embeddings, search     (Part 2)
  metrics/          # deterministic finance math       (Part 3)
  agent/            # tools, loop, citations           (Part 3)
  workflows/        # comps, tearsheet, exports        (Part 4)
  evals/            # golden sets, graders, runner     (Part 5)
  api/              # FastAPI app                      (Part 6)
web/                # React UI                         (Part 6)
tests/
docs/parts/         # specs — read, don't edit unless the user asks
PROGRESS.md         # you update this
```
