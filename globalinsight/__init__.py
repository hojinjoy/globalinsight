"""GlobalInsight: advisor briefing backend.

Fetches a stock quote plus SEC filings for a ticker and synthesizes a cited
brief. See globalinsight.pipeline for the orchestration entry points
(wave2, wave3, build_brief) and globalinsight.__main__ for the CLI.

No network calls or client construction happen at import time anywhere in
this package - see http.py and synthesize.py.
"""
