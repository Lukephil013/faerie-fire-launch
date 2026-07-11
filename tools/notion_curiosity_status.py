"""Diagnose the curiosity -> Notion sync path end to end, using the exact
same function gui.py calls (sync_curiosity_to_notion), but with the result
printed instead of swallowed — so a silent failure becomes visible.

Usage: python tools/notion_curiosity_status.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.config import load  # noqa: E402
from livingpc.curiosity import CuriosityStore, get_curiosity_model  # noqa: E402
from livingpc.inference import InferenceStore  # noqa: E402
from livingpc.memory import MemoryStore  # noqa: E402
from livingpc.notion_sync import sync_curiosity_to_notion  # noqa: E402


def main() -> int:
    cfg = load("config.toml")
    print(f"notion_sync_enabled = {cfg.notion_sync_enabled}")
    print(f"notion_api_key set  = {bool(cfg.notion_api_key)}")
    print(f"notion_parent_page_id = {cfg.notion_parent_page_id}")
    print(f"notion_curiosity_database_id = {cfg.notion_curiosity_database_id or '(legacy mode)'}")
    print(f"notion_curiosity_data_source_id = "
          f"{cfg.notion_curiosity_data_source_id or '(auto-resolve single source)'}")
    print(f"notion_curiosity_covers = {len(cfg.notion_curiosity_cover_file_upload_ids)} configured")
    print(f"curiosity_metrics_enabled = {cfg.curiosity_metrics_enabled}")
    print(f"curiosity_calibration_days = {cfg.curiosity_calibration_days}")
    print()

    mem = MemoryStore(cfg.memory_db_path)
    inf = InferenceStore(cfg.memory_db_path)
    store = CuriosityStore(cfg.memory_db_path)
    try:
        rows = store.list_curiosities()
        if not rows:
            print("No curiosities exist at all in the database.")
            print("(This means there's nothing for 'Generate more' to act on — "
                  "add a goal/curiosity in the Curiosity tab first.)")
            return 0

        print(f"Found {len(rows)} curiosities:\n")
        model = get_curiosity_model(cfg)
        for row in rows:
            print(f"- id={row['id']} label={row['label']!r} status={row['status']} "
                  f"notion_page_id={row.get('notion_page_id')}")

        print("\nAttempting a real sync for each one (this will actually "
              "create/update Notion pages if it succeeds):\n")
        for row in rows:
            result = sync_curiosity_to_notion(cfg, mem, inf, store, row["id"], model)
            status = "OK" if result.get("ok") else "FAILED"
            detail = result.get("page_id") or result.get("message")
            print(f"[{status}] id={row['id']} label={row['label']!r} -> {detail}")
    finally:
        mem.close()
        inf.close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
