---
description: Run every quality check (lint, format, types, migrations, tests) and fix failures
---

Run these in order and report a pass/fail table:

```
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run alembic check
uv run pytest -rs
```

If something fails, fix it, re-run the full list, and show the final table.
Skip any check whose tool is not set up yet (e.g. alembic before Part 1) and say so.
