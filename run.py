#!/usr/bin/env python3
"""Start the bot.

    python run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `import src...` work no matter which directory you run this from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.bot.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
