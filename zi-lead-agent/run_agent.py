"""Launcher that works without `pip install` (handy for Task Scheduler).

    python run_agent.py run-daily
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from zi_agent.cli import main  # noqa: E402

raise SystemExit(main())
