"""Verify the Notion integration is reachable and can see the configured
parent page — run this right after setting notion_api_key in config.toml,
instead of finding out it's broken only after starting a real curiosity.

Usage: python tools/check_notion.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.config import load  # noqa: E402
from livingpc.notion_sync import NotionClient, NotionError  # noqa: E402


def main() -> int:
    cfg = load("config.toml")
    if not cfg.notion_api_key:
        print("notion_api_key is empty in config.toml — nothing to check.")
        print('Add: notion_api_key = "ntn_..." (must be quoted — it\'s a TOML string).')
        return 1
    if not cfg.notion_parent_page_id:
        print("notion_parent_page_id is empty in config.toml — nothing to check.")
        return 1

    client = NotionClient(cfg.notion_api_key)
    try:
        page = client.retrieve_page(cfg.notion_parent_page_id)
    except NotionError as error:
        print(f"FAILED: {error}")
        print("Check: the token was copied correctly, and the page has been shared "
              "with your integration (open the page in Notion -> \"...\" menu -> "
              "Connections -> add your integration).")
        return 1

    title_parts = page.get("properties", {}).get("title", {}).get("title", [])
    title = "".join(t.get("plain_text", "") for t in title_parts) or "(untitled)"
    print(f'OK — your integration can see the page: "{title}"')
    print("Notion sync is ready: starting or updating a curiosity will now "
          "create/update a page under it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
