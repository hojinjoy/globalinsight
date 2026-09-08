#!/usr/bin/env sh
# GlobalInsight web UI. Serves the API and the static front end on one port.
#
#   ./run.sh                 live mode (needs API credits for the narrative)
#   GI_FIXTURES=1 ./run.sh   replay mode, zero credits
#
# GI_FIXTURE_SPEED=10 replays recorded timings 10x faster for quick test runs.
exec .venv/bin/uvicorn api.app:app --host 0.0.0.0 --port "${PORT:-8520}"
