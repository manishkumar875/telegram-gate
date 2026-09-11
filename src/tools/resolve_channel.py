"""Turn a YouTube @handle into the UC... channel ID needed for Phase 2.

    python -m src.tools.resolve_channel
    python -m src.tools.resolve_channel https://www.youtube.com/@somechannel

Needs YOUTUBE_API_KEY in .env (a free API key from Google Cloud Console;
no OAuth and no billing required for this lookup).
"""

from __future__ import annotations

import asyncio
import os
import sys

from src.config.settings import extract_channel_handle, extract_channel_id
from src.verification.youtube_oauth import resolve_channel_id


async def _run(url: str, api_key: str) -> int:
    direct = extract_channel_id(url)
    if direct:
        print("\nThat URL already contains the channel ID.\n")
        print(f"    YOUTUBE_CHANNEL_ID={direct}\n")
        return 0

    from src.verification.youtube_oauth import resolve_channel_id_from_page

    handle = extract_channel_handle(url)
    channel_id = None
    source = ""

    if api_key and handle:
        print(f"Looking up {handle} via the YouTube Data API ...")
        channel_id = await resolve_channel_id(api_key=api_key, handle=handle)
        source = "YouTube Data API"

    if not channel_id:
        # No API key required: the id is public in the channel page HTML.
        print("Reading the public channel page ...")
        channel_id = await resolve_channel_id_from_page(url)
        source = "public channel page"

    if not channel_id:
        print(
            "\nCould not determine the channel ID.\n\n"
            "Check the URL is right and the channel is public. To find it by\n"
            'hand: open your channel, View Page Source, search for "externalId"\n'
            "- the UC... value next to it is your channel ID.",
            file=sys.stderr,
        )
        return 1

    print("\n" + "=" * 60)
    if handle:
        print(f"  HANDLE     : {handle}")
    print(f"  CHANNEL ID : {channel_id}")
    print(f"  SOURCE     : {source}")
    print("=" * 60)
    print("\nSave it with:\n")
    print(f"    python scripts/setup_wizard.py --set YOUTUBE_CHANNEL_ID={channel_id}\n")
    return 0


def main() -> int:
    from dotenv import load_dotenv

    from src.config.settings import PROJECT_ROOT

    load_dotenv(PROJECT_ROOT / ".env")

    url = sys.argv[1] if len(sys.argv) > 1 else (os.getenv("YOUTUBE_CHANNEL_URL") or "")
    url = url.strip().strip("'\"")
    if not url:
        print(
            "Usage: python -m src.tools.resolve_channel <your channel URL>\n"
            "(or set YOUTUBE_CHANNEL_URL in .env first)",
            file=sys.stderr,
        )
        return 2

    api_key = (os.getenv("YOUTUBE_API_KEY") or "").strip().strip("'\"")
    return asyncio.run(_run(url, api_key))


if __name__ == "__main__":
    raise SystemExit(main())
