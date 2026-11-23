# Part 6 — Ship it (API, web UI, Docker, deploy, observability, demo)

## Goal
Make it a product someone at Rogo can open from a link in your application and use in
two minutes.

## Build
1. **API** `analyst/api/` (FastAPI):
   - `POST /ask` → streams (SSE) agent steps then the final `Answer` with citations.
   - `POST /workflows/{type}` → starts a run (background task), `GET /workflows/{id}`
     → status + download links.
   - `GET /citations/resolve` → exact source text + SEC URL for a citation.
   - `GET /companies`, `GET /health`.
   - API-key auth header, per-key rate limit, request IDs, structured JSON logs.
2. **Web UI** `web/` (React + Vite + TS, Tailwind):
   - Ask view: question box, live "agent is working" step list, answer with clickable
     footnotes that open a side panel showing the exact cited passage highlighted, plus a
     link to the filing on sec.gov.
   - Workflows view: pick comps / tearsheet / earnings, pick ticker, download outputs.
   - Run history with cost and latency per run. Clean, dense, finance-grade design.
3. **Observability**: OpenTelemetry-style traces per request stored in Postgres; an admin
   page showing queries/day, p95 latency, $ spend, eval baseline scores.
4. **Packaging**: multi-stage `Dockerfile` for API (+ static web build), `docker compose`
   for local full stack, seed script that ingests a small universe so a fresh clone works.
5. **Deploy**: Render or Fly.io (API + managed Postgres with pgvector) and the web build.
   Document env vars and a monthly cost estimate. Guard spend: daily $ cap env var.
6. **CI**: the `docker` job in `.github/workflows/ci.yml` turns on once the Dockerfile
   exists; make it pass. Add an `npm` entry for `/web` to `.github/dependabot.yml`.
7. **Demo assets**: README top section with a 60-second GIF, a "Try it" link, the eval
   headline numbers, architecture diagram, and "What I'd build next at Rogo" (e.g. CRM
   connectors, Excel add-in, transcript ingestion, private-company data).

## Acceptance checks
- [ ] `docker compose up` from a fresh clone gives a working app at localhost.
- [ ] Deployed URL works; README links to it.
- [ ] Playwright smoke test: ask a question, see an answer with ≥ 1 citation, open it.
- [ ] README polished top to bottom; PROGRESS.md shows all 6 parts done.

## Prompt for Maxx to paste into Claude Code
> Read CLAUDE.md and PROGRESS.md, then build Part 6 exactly as specified in
> docs/parts/PART-6.md. Run every acceptance check, record results in PROGRESS.md,
> commit, and tell me what I need to do by hand (accounts, deploy secrets).
