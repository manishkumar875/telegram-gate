#!/usr/bin/env python3
"""Open a public https:// tunnel to your local bot and save the URL to .env.

    python scripts/tunnel.py

Phase 2 needs Google to be able to reach your machine over https. This starts
a Cloudflare "quick tunnel", which needs **no account and no credit card**,
then does the fiddly bit for you: it writes the URL into .env as
OAUTH_PUBLIC_BASE_URL and prints the exact redirect URI to register in Google
Cloud.

Important: a quick tunnel gets a NEW random URL every time it starts. Each
time you restart it you must update the Authorised redirect URI in Google
Cloud to match. This script prints exactly what to paste. For something
permanent, deploy to a host with a stable address instead (see the README).

Run this in ONE terminal, and `python run.py` in a SECOND terminal.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

QUICK_TUNNEL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

CANDIDATE_PATHS = [
    Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
    Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
    Path("/usr/local/bin/cloudflared"),
    Path("/usr/bin/cloudflared"),
]


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for candidate in CANDIDATE_PATHS:
        if candidate.is_file():
            return str(candidate)
    return None


def local_port() -> int:
    from src.config.settings import _env  # noqa: PLC2701 - internal helper is fine here

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    raw = (os.getenv("WEB_PORT") or "").strip() or "8080"
    try:
        return int(raw)
    except ValueError:
        return 8080


def port_is_open(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    from src.console import force_utf8_console

    force_utf8_console()

    binary = find_cloudflared()
    if not binary:
        print(
            "cloudflared is not installed.\n\n"
            "Install it (no account needed):\n"
            "    winget install --id Cloudflare.cloudflared -e\n\n"
            "Or download it from:\n"
            "    https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/\n",
            file=sys.stderr,
        )
        return 1

    port = local_port()
    print("=" * 70)
    print("  PUBLIC HTTPS TUNNEL")
    print("=" * 70)
    print(f"  cloudflared : {binary}")
    print(f"  forwarding  : http://localhost:{port}")
    if not port_is_open(port):
        print(f"\n  NOTE: nothing is listening on port {port} yet.")
        print("  Start the bot in another terminal:  python run.py")
        print("  The tunnel will work as soon as it comes up.")
    print()

    process = subprocess.Popen(
        [binary, "tunnel", "--url", f"http://localhost:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    public_url: str | None = None
    deadline = time.monotonic() + 60

    try:
        assert process.stdout is not None
        for line in process.stdout:
            if public_url is None:
                match = QUICK_TUNNEL_RE.search(line)
                if match:
                    public_url = match.group(0)
                    _on_url(public_url)
                elif time.monotonic() > deadline:
                    print(
                        "  Could not obtain a tunnel URL within 60 seconds.\n"
                        "  Raw output follows:\n",
                        file=sys.stderr,
                    )
                    print(line, end="", file=sys.stderr)
            # Surface errors, stay quiet otherwise.
            if "ERR" in line or "error" in line.lower():
                print(f"  cloudflared: {line.strip()}")
            if process.poll() is not None:
                break
    except KeyboardInterrupt:
        print("\n  Stopping tunnel...")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    print("  Tunnel closed.")
    if public_url:
        print("\n  Reminder: OAUTH_PUBLIC_BASE_URL in .env still points at the")
        print("  tunnel that just closed. Run this script again for a new URL,")
        print("  and update the redirect URI in Google Cloud to match.")
    return 0


def _on_url(public_url: str) -> None:
    from scripts.setup_wizard import write_env_value

    print("=" * 70)
    print("  TUNNEL IS UP")
    print("=" * 70)
    print(f"\n  Public URL : {public_url}")

    try:
        write_env_value("OAUTH_PUBLIC_BASE_URL", public_url)
        print("  Saved to .env as OAUTH_PUBLIC_BASE_URL")
    except SystemExit as exc:
        print(f"  Could not write to .env: {exc}")

    print("\n" + "-" * 70)
    print("  PASTE THIS INTO GOOGLE CLOUD")
    print("-" * 70)
    print("  APIs & Services > Credentials > your OAuth client >")
    print("  'Authorised redirect URIs' > ADD URI:\n")
    print(f"      {public_url}/oauth/callback\n")
    print("  It must match character for character - no trailing slash.")
    print("-" * 70)
    print("\n  Now start the bot in ANOTHER terminal:   python run.py")
    print("  Keep THIS window open - closing it kills the tunnel.\n")


if __name__ == "__main__":
    raise SystemExit(main())
