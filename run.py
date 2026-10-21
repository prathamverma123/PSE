#!/usr/bin/env python
"""Runner: python run.py — starts the app on 127.0.0.1:5000."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app as app_module


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    app = app_module.create_app()
    cfg = app.config
    host = "127.0.0.1"
    port = int(cfg.get("PORT", 5000))
    print(f" * Personal Search Engine running on http://{host}:{port}")
    print(" * Press CTRL+C to stop. All data stays on this machine.")
    app.run(host=host, port=port, debug=bool(cfg.get("DEBUG")), use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
