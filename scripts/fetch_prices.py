"""Refresh data/prices.csv from Yahoo Finance (see analyst/metrics/yahoo.py).

uv run python scripts/fetch_prices.py
"""

import sys

from analyst.metrics.yahoo import main

if __name__ == "__main__":
    sys.exit(main())
