"""Console helpers.

Deliberately imports nothing but the standard library, so it is safe to use
before the dependency check in ``scripts/preflight.py`` has run.
"""

from __future__ import annotations

import sys


def force_utf8_console() -> None:
    """Stop Windows' legacy code page from mangling non-ASCII output.

    On a default Windows console (code page 1252) printing characters such as
    an ellipsis or an emoji either raises ``UnicodeEncodeError`` or prints
    mojibake. Reconfiguring to UTF-8 with ``errors="replace"`` makes output
    predictable everywhere without ever crashing the program.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - exotic terminals
            pass
