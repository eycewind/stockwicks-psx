# app/scripts/replay/__init__.py
"""
Replay subsystem.

Completely separate from live paper-trade flow.

Modules:
  data_ingest           — fetch 30 days of 1-min bars from Schwab, save to user dir
  replay_data_provider  — load 1-min CSV, filter by date range, resample on the fly
  orchestrator          — (step 4) walks bars at requested speed, calls replay runner
"""