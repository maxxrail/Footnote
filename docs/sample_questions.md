# Sample questions (Part 3 acceptance)

The live test `tests/test_agent_live.py` asks each of these against the real corpus and
the real model (`uv run pytest -m live tests/test_agent_live.py -s`). Results are pasted,
trimmed, into `PROGRESS.md`.

## Answered with citations

Each must come back with citations, no unverified or uncited numbers, and filing citations
that resolve to their exact text.

1. How did NVIDIA's operating margin trend over the last three fiscal years, and what did management attribute the change to?
2. What was Microsoft's revenue growth in its latest fiscal year, and what does the MD&A say drove it?
3. Compare the latest fiscal-year operating margins of Apple, Microsoft and Oracle, and give the peer median.
4. What does JPMorgan's latest 10-K say about interest rate risk, and what was its net income in the last two fiscal years?
5. What was Intel's revenue CAGR over the last three fiscal years, and did it report a net loss in any of them?

## Refusals

Each must come back `refused: true`, with no number that a tool didn't return.

- R1 (company not in the universe): What was Tesla's revenue in fiscal 2025?
- R2 (fact not in the filings): What was Goldman Sachs's gross profit in its latest fiscal year?
