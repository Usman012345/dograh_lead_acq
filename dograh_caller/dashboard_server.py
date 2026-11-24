#!/usr/bin/env python3
"""
Dashboard server for dograh_caller.

Reads output/pipeline_tracker.xlsx (or --tracker), serves the FastAPI JSON API
and pre-built React UI (dashboard/static), and provides Start/Stop batch runner controls.

Usage:
    python dashboard_server.py                 # the live pipeline
    python dashboard_server.py --demo          # fictional data, safe to show anyone
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

import uvicorn

import config
from dashboard.app import create_app

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

NON_LOOPBACK_WARNING = (
    "WARNING: this dashboard has no login. Anyone who can reach this address "
    "can see lead details, phone numbers, transcripts and recordings."
)


def warn_if_non_loopback(host: str) -> bool:
    if host.strip().lower() not in _LOOPBACK_HOSTS:
        print(NON_LOOPBACK_WARNING, flush=True)
        return True
    return False


def allowed_hosts_for(bind_host: str) -> Optional[List[str]]:
    """A dashboard bound to this computer only answers to this computer's own names."""
    if bind_host.strip().lower() in _LOOPBACK_HOSTS:
        return sorted(_LOOPBACK_HOSTS)
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start the dograh_caller dashboard server (stays idle until you start a batch run)."
    )
    parser.add_argument("--host", default=config.DASHBOARD_HOST, help=f"Bind host (default {config.DASHBOARD_HOST})")
    parser.add_argument("--port", type=int, default=config.DASHBOARD_PORT, help=f"Bind port (default {config.DASHBOARD_PORT})")
    parser.add_argument(
        "--tracker",
        type=Path,
        default=None,
        help=f"Path to the tracker workbook (default {config.PIPELINE_TRACKER_FILE})",
    )
    parser.add_argument("--demo", action="store_true",
                        help="Show fictional demo leads, transcripts and audio instead of real data")
    args = parser.parse_args(argv)

    media = None
    if args.demo:
        from dashboard.demo_media import DemoMedia
        from tools.make_demo_tracker import generate_demo_tracker

        tracker = args.tracker or config.PROJECT_DIR / "output" / "demo_tracker.xlsx"
        generate_demo_tracker(tracker)
        media = DemoMedia()
    else:
        tracker = args.tracker or config.PIPELINE_TRACKER_FILE

    warn_if_non_loopback(args.host)
    print(f"Dashboard reading {'FICTIONAL DEMO DATA from ' if args.demo else 'tracker: '}{tracker}", flush=True)
    print(f"Dashboard listening on http://{args.host}:{args.port}", flush=True)
    if args.demo:
        print("Batch runner is idle (DEMO mode: Start Batch runs an offline simulation).", flush=True)
    else:
        print(f"Lead source (reverse proxy): {config.LEAD_SOURCE_URL}", flush=True)
        print("Batch runner is idle. Open the dashboard and click 'Start Batch' to pull and call leads.", flush=True)

    app = create_app(tracker_path=tracker, media=media, demo=args.demo,
                     allowed_hosts=allowed_hosts_for(args.host))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
