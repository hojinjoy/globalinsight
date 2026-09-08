"""Refresh the SQLite ticker/CIK store (globalinsight.store) from SEC.

Intended for cron, once a day:

    0 6 * * *  cd /path/to/globalinsight && uv run python -m scripts.refresh_tickers

Idempotent: safe to run as often as you like. It's a plain upsert against
SEC's company_tickers.json (present -> upsert + last_seen=today + active=1;
absent -> soft delete, active=0, never a real DELETE - see store.py's
module docstring for why). The one thing it will refuse to do is wipe the
table: if the fetch comes back with fewer than ~90% of the tickers
currently active, that's treated as a truncated or errored SEC response and
the refresh aborts loudly instead of silently soft-deleting almost
everything.
"""

import sys

from globalinsight import store


def main() -> int:
    report = store.refresh()

    if report.aborted:
        print(f"REFRESH ABORTED: {report.reason}", file=sys.stderr)
        return 1

    print(
        f"refresh ok: fetched={report.fetched} upserted={report.upserted} "
        f"deactivated={report.deactivated} active_total={store.row_count()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
