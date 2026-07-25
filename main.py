"""Command-line entry point for wxread automation."""

from __future__ import annotations

import logging

from reader import ReaderEvent, ReaderConfig, run_reading


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)-8s - %(message)s",
)
logger = logging.getLogger(__name__)


def log_event(event: ReaderEvent) -> None:
    level = getattr(logging, event.level.upper(), logging.INFO)
    logger.log(level, event.message)


def main() -> int:
    result = run_reading(ReaderConfig(), progress_callback=log_event)
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
