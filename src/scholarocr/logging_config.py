"""Logging setup. Logs go to stderr (stdout is reserved for MCP stdio / CLI JSON output).

Never log document text or API keys: log identifiers, counts and timings only.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    root = logging.getLogger("scholarocr")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.propagate = False
        _CONFIGURED = True
    # The SDKs can be chatty at INFO and may include request details.
    for noisy in ("httpx", "httpcore", "google_genai", "google.genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"scholarocr.{name}")
