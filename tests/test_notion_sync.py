"""Notion sync — markdown->blocks conversion and the best-effort orchestration
in sync_curiosity_to_notion. Real HTTP calls are never exercised here; a fake
client is injected wherever the orchestration needs one."""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LIVINGPC_DB_KEY", None)

from livingpc.config import Config  # noqa: E402
from livingpc.curiosity import CuriosityStore, StubCuriosityModel  # noqa: E402
from livingpc.inference import InferenceStore  # noqa: E402
from livingpc.memory import MemoryStore  # noqa: E402
from livingpc.notion_sync import (  # noqa: E402
    NOTION_FILE_VERSION, NotionClient, NotionError, curiosity_cover_file_upload_id,
    curiosity_database_properties, markdown_to_blocks, metric_dashboard_blocks,
    sync_curiosity_to_notion,
)


class _FakeNotionClient:
    """Records calls instead of hitting the network; lets tests assert on
    exactly what sync_curiosity_to_notion would have sent to Notion."""

    def __init__(self, existing_page_id=None):
        self.created = []     # (parent_page_id, title, blocks)
        self.replaced = []    # (page_id, title, blocks)
        self.database_created = []
        self.database_updated = []
        self.database_pages = set()
        self.covers = []
        self.uploads = []
        self.schemas = []
        self._next_id = "generated-page-id"

    def resolve_data_source(self, database_id, configured_id=""):
        return configured_id or "data-source-123"

    def ensure_metric_properties(self, data_source_id):
        self.schemas.append(data_source_id)

    def create_page(self, parent_page_id, title, blocks):
        self.created.append((parent_page_id, title, blocks))
        return self._next_id

    def replace_page_content(self, page_id, title, blocks):
        self.replaced.append((page_id, title, blocks))

    def page_belongs_to_database(self, page_id, database_id):
        return page_id in self.database_pages

    def create_database_page(self, database_id, properties, blocks):
        self.database_created.append((database_id, properties, blocks))
        self.database_pages.add(self._next_id)
        return self._next_id

    def update_database_page(self, page_id, properties, blocks):
        self.database_updated.append((page_id, properties, blocks))

    def set_page_cover(self, page_id, file_upload_id):
        self.covers.append((page_id, file_upload_id))

    def upload_file(self, path):
        self.uploads.append(path)
        return "chart-upload-id"


class TestMarkdownToBlocks(unittest.TestCase):
    def test_headings_bullets_and_paragraphs(self):
        md = "# Goal\nSome paragraph.\n- point one\n* point two\n## Direction\nKeep going."
        blocks = markdown_to_blocks(md)
        kinds = [b["type"] for b in blocks]
        self.assertEqual(kinds, ["heading_1", "paragraph", "bulleted_list_item",
                                 "bulleted_list_item", "heading_2", "paragraph"])
        self.assertEqual(
            blocks[0]["heading_1"]["rich_text"][0]["text"]["content"], "Goal")
        self.assertEqual(
            blocks[2]["bulleted_list_item"]["rich_text"][0]["text"]["content"], "point one")

    def test_blank_lines_are_skipped(self):
        blocks = markdown_to_blocks("line one\n\n\nline two")
        self.assertEqual(len(blocks), 2)

    def test_empty_markdown_yields_placeholder_block(self):
        blocks = markdown_to_blocks("")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "paragraph")


class TestRetrievePage(unittest.TestCase):
    """retrieve_page is the reachability/permission check tools/check_notion.py
    uses — no live network call here, just that it hits the right endpoint
    and surfaces NotionError the same way the other calls do."""

    def test_delegates_to_get_pages_endpoint(self):
        client = NotionClient("fake-token")
        calls = []
        client._request = lambda method, path, body=None: (
            calls.append((method, path)) or {"id": "page-1", "properties": {}})
        result = client.retrieve_page("page-1")
        self.assertEqual(calls, [("GET", "/pages/page-1")])
        self.assertEqual(result["id"], "page-1")

    def test_propagates_notion_error(self):
        client = NotionClient("bad-token")

        def _boom(method, path, body=None):
            raise NotionError("Notion API 401: unauthorized")
        client._request = _boom
        with self.assertRaises(NotionError):
            client.retrieve_page("page-1")

    def test_database_parent_comparison_ignores_uuid_dashes(self):
        client = NotionClient("fake-token")
        client.retrieve_page = lambda page_id: {
            "parent": {"type": "database_id", "database_id": "abc-def"}}
        self.assertTrue(client.page_belongs_to_database("page-1", "abcdef"))

    def test_page_cover_uses_current_files_api_version(self):
        client = NotionClient("fake-token")
        calls = []
        client._request = lambda method, path, body=None, **kwargs: (
            calls.append((method, path, body, kwargs)) or {})
        client.set_page_cover("page-1", "upload-1")
        self.assertEqual(calls[0][0:2], ("PATCH", "/pages/page-1"))
        self.assertEqual(calls[0][2]["cover"]["file_upload"]["id"], "upload-1")
        self.assertEqual(calls[0][3]["version"], NOTION_FILE_VERSION)

    def test_resolves_single_data_source_and_adds_only_missing_metric_properties(self):
        client = NotionClient("fake-token")
        calls = []
        def fake_request(method, path, body=None, **kwargs):
            calls.append((method, path, body))
            if path == "/databases/db":
                return {"data_sources": [{"id": "source-1"}]}
            if path == "/data_sources/source-1" and method == "GET":
                return {"properties": {"Level": {"number": {}}}}
            return {}
        client._request = fake_request
        self.assertEqual(client.resolve_data_source("db"), "source-1")
        client.ensure_metric_properties("source-1")
        patch = next(body for method, path, body in calls
                     if method == "PATCH" and path == "/data_sources/source-1")
        self.assertNotIn("Level", patch["properties"])
        self.assertIn("XP", patch["properties"])

    def test_multiple_data_sources_require_explicit_configuration(self):
        client = NotionClient("fake-token")
        client.retrieve_database = lambda _: {"data_sources": [{"id": "a"}, {"id": "b"}]}
        with self.assertRaises(NotionError):
            client.resolve_data_source("db")


class TestMetricDashboardBlocks(unittest.TestCase):
    def test_uses_mastery_key_for_bars_and_recommendation(self):
        profile = SimpleNamespace(
            state_metrics=[], dimensions=[SimpleNamespace(
                slug="focus", label="Focus", checkin_prompt="Try one focused block")])
        snapshot = SimpleNamespace(
            level=2, total_xp=125, xp_into_level=25, overall_mastery=70,
            overall_confidence=.5, trend_7d=2, evidence_count=1,
            snapshot_date="2026-07-05", state={}, summary="Stable.",
            metrics={"focus": {"mastery": 70, "confidence": .5}})
        blocks = metric_dashboard_blocks(profile, snapshot, [], None)
        text = " ".join(
            block[block["type"]]["rich_text"][0]["text"]["content"]
            for block in blocks if block["type"] != "image")
        self.assertIn("70/100", text)
        self.assertIn("Try one focused block", text)
        self.assertNotIn("Focus: unknown", text)

    def test_database_update_replaces_only_managed_toggle_children(self):
        client = NotionClient("fake-token")
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path, body))
            if method == "GET" and path.startswith("/blocks/page-1/children"):
                return {"results": [
                    {"id": "user-block", "type": "paragraph", "paragraph": {}},
                    {"id": "managed", "type": "toggle", "toggle": {
                        "rich_text": [{"plain_text": "Living Computer — Synced"}]}}
                ], "has_more": False}
            if method == "GET" and path.startswith("/blocks/managed/children"):
                return {"results": [{"id": "old-managed-child"}], "has_more": False}
            return {}

        client._request = fake_request
        client.update_database_page(
            "page-1", {"Status": {"select": {"name": "Active"}}},
            markdown_to_blocks("new summary"))
        deleted = [path for method, path, _ in calls if method == "DELETE"]
        self.assertEqual(deleted, ["/blocks/old-managed-child"])
        self.assertNotIn("/blocks/user-block", deleted)
        self.assertTrue(any(
            method == "PATCH" and path == "/blocks/managed/children"
            for method, path, _ in calls))

    def test_database_update_recreates_missing_managed_toggle(self):
        client = NotionClient("fake-token")
        calls = []

        def fake_request(method, path, body=None):
            calls.append((method, path, body))
            if method == "GET":
                return {"results": [
                    {"id": "user-block", "type": "paragraph", "paragraph": {}}
                ], "has_more": False}
            return {}

        client._request = fake_request
        client.update_database_page("page-1", {}, markdown_to_blocks("new summary"))
        append_calls = [body for method, path, body in calls
                        if method == "PATCH" and path == "/blocks/page-1/children"]
        self.assertEqual(len(append_calls), 1)
        toggle = append_calls[0]["children"][0]
        self.assertEqual(toggle["type"], "toggle")
        self.assertEqual(
            toggle["toggle"]["rich_text"][0]["text"]["content"],
            "Living Computer — Synced")


class TestDatabaseProperties(unittest.TestCase):
    def test_maps_curiosity_to_life_hub_schema(self):
        props = curiosity_database_properties({
            "id": 7, "label": "fitness", "status": "paused", "is_greatest": True,
        }, synced_at="2026-07-05T12:00:00+00:00")
        self.assertEqual(props["Name"]["title"][0]["text"]["content"], "fitness")
        self.assertEqual(props["Status"]["select"]["name"], "Paused")
        self.assertEqual(props["Focus"]["select"]["name"], "Greatest")
        self.assertEqual(props["Last Synced"]["date"]["start"],
                         "2026-07-05T12:00:00+00:00")
        self.assertEqual(props["Local Curiosity ID"]["number"], 7)

    def test_maps_qualified_metric_snapshot(self):
        snapshot = SimpleNamespace(
            level=3, total_xp=240, overall_mastery=62.5,
            overall_confidence=.72, trend_7d=4.25, snapshot_date="2026-07-05")
        props = curiosity_database_properties({
            "id": 7, "label": "fitness", "status": "active", "is_greatest": False,
        }, snapshot=snapshot)
        self.assertEqual(props["Level"]["number"], 3)
        self.assertEqual(props["Metric Confidence"]["number"], 72.0)
        self.assertEqual(props["Last Snapshot"]["date"]["start"], "2026-07-05")

    def test_cover_selection_is_stable_and_cycles(self):
        cfg = Config()
        cfg.notion_curiosity_cover_file_upload_ids = ["cover-a", "cover-b", "cover-c"]
        self.assertEqual(curiosity_cover_file_upload_id(cfg, 1), "cover-a")
        self.assertEqual(curiosity_cover_file_upload_id(cfg, 2), "cover-b")
        self.assertEqual(curiosity_cover_file_upload_id(cfg, 4), "cover-a")

    def test_cover_selection_can_be_disabled(self):
        cfg = Config()
        cfg.notion_curiosity_cover_file_upload_ids = []
        self.assertIsNone(curiosity_cover_file_upload_id(cfg, 1))


class TestSyncCuriosityToNotion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(self.tmp.name, "memory.db")
        self.mem = MemoryStore(db)
        self.inf = InferenceStore(db)
        self.store = CuriosityStore(db)
        self.model = StubCuriosityModel()
        self.cid = self.store.add_curiosity("help me get fit", "fitness")
        self.cfg = Config(db_path=os.path.join(self.tmp.name, "e.db"), memory_db_path=db)

    def tearDown(self):
        self.mem.close()
        self.inf.close()
        self.store.close()
        self.tmp.cleanup()

    def _calibrate_and_publish(self):
        from livingpc.curiosity_metrics import MetricStore
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            profile = metrics.ensure_profile(self.store.get_curiosity(self.cid))
            metrics.approve_profile(
                self.cid, dimensions=profile.dimensions, state_metrics=profile.state_metrics)
            for offset in range(7):
                day = (date(2026, 6, 29) + timedelta(days=offset)).isoformat()
                metrics.record_checkin(
                    self.cid, {"energy": 5}, {"consistency": 5}, checkin_date=day)
                metrics.build_snapshot(self.cid, day)
            metrics.approve_publication(self.cid)
        finally:
            metrics.close()

    def test_not_configured_without_token_returns_ok_false(self):
        self.cfg.notion_api_key = ""
        self.cfg.notion_parent_page_id = "some-parent"
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model)
        self.assertFalse(result["ok"])
        self.assertIn("not configured", result["message"])

    def test_not_configured_without_parent_returns_ok_false(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_parent_page_id = ""
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model)
        self.assertFalse(result["ok"])

    def test_disabled_flag_short_circuits(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_parent_page_id = "some-parent"
        self.cfg.notion_sync_enabled = False
        client = _FakeNotionClient()
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model, client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(client.created, [])

    def test_first_sync_creates_a_page_and_stores_its_id(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_parent_page_id = "parent-123"
        client = _FakeNotionClient()
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model, client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(result["page_id"], "generated-page-id")
        self.assertEqual(len(client.created), 1)
        parent, title, blocks = client.created[0]
        self.assertEqual(parent, "parent-123")
        self.assertEqual(title, "fitness")
        self.assertTrue(blocks)
        self.assertEqual(
            self.store.get_curiosity(self.cid)["notion_page_id"], "generated-page-id")

    def test_second_sync_replaces_content_on_the_same_page(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_parent_page_id = "parent-123"
        client = _FakeNotionClient()
        sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model, client=client)
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model, client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(len(client.created), 1)     # only the first call created
        self.assertEqual(len(client.replaced), 1)    # the second replaced instead
        page_id, title, _ = client.replaced[0]
        self.assertEqual(page_id, "generated-page-id")
        self.assertEqual(title, "fitness")

    def test_database_mode_creates_row_and_stores_new_mapping(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_curiosity_database_id = "database-123"
        self._calibrate_and_publish()
        client = _FakeNotionClient()
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model, client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(len(client.database_created), 1)
        database_id, properties, blocks = client.database_created[0]
        self.assertEqual(database_id, "data-source-123")
        self.assertEqual(properties["Status"]["select"]["name"], "Active")
        self.assertEqual(properties["Focus"]["select"]["name"], "Background")
        self.assertTrue(blocks)
        self.assertEqual(self.store.get_curiosity(self.cid)["notion_page_id"],
                         "generated-page-id")
        self.assertEqual(client.covers, [
            ("generated-page-id", self.cfg.notion_curiosity_cover_file_upload_ids[0])])

    def test_supported_metric_curiosity_does_not_create_row_before_approval(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_curiosity_database_id = "database-123"
        client = _FakeNotionClient()
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model, client=client)
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(client.database_created, [])
        self.assertIsNone(self.store.get_curiosity(self.cid)["notion_page_id"])

    def test_database_mode_updates_existing_database_row(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_curiosity_database_id = "database-123"
        self._calibrate_and_publish()
        self.store.set_notion_page_id(self.cid, "existing-row")
        client = _FakeNotionClient()
        client.database_pages.add("existing-row")
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model, client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(len(client.database_updated), 1)
        self.assertEqual(client.database_updated[0][0], "existing-row")
        self.assertEqual(client.database_created, [])
        self.assertEqual(client.covers, [
            ("existing-row", self.cfg.notion_curiosity_cover_file_upload_ids[0])])

    def test_database_dashboard_requires_explicit_publish_after_calibration(self):
        from livingpc.curiosity_metrics import MetricStore

        self.cfg.notion_api_key = "secret"
        self.cfg.notion_curiosity_database_id = "database-123"
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            row = self.store.get_curiosity(self.cid)
            profile = metrics.ensure_profile(row)
            metrics.approve_profile(
                self.cid, dimensions=profile.dimensions, state_metrics=profile.state_metrics)
            start = date(2026, 6, 29)
            for offset in range(7):
                day = (start + timedelta(days=offset)).isoformat()
                metrics.record_checkin(
                    self.cid, {"energy": 4}, {"consistency": 4}, checkin_date=day)
                metrics.build_snapshot(self.cid, day)
            private_result = sync_curiosity_to_notion(
                self.cfg, self.mem, self.inf, self.store, self.cid, self.model,
                client=_FakeNotionClient())
            self.assertTrue(private_result["ok"])
            self.assertTrue(private_result["skipped"])
            metrics.approve_publication(self.cid)
        finally:
            metrics.close()

        client = _FakeNotionClient()
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model, client=client)
        self.assertTrue(result["ok"])
        _, properties, blocks = client.database_created[0]
        self.assertEqual(properties["Last Snapshot"]["date"]["start"], "2026-07-05")
        self.assertEqual(len(client.uploads), 1)
        self.assertTrue(any(block["type"] == "image" for block in blocks))
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            self.assertIsNotNone(metrics.get_profile(self.cid).last_published_at)
        finally:
            metrics.close()

        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model, client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(len(client.uploads), 1)

    def test_failed_page_attachment_does_not_persist_fresh_upload_id(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_curiosity_database_id = "database-123"
        self._calibrate_and_publish()

        class FailingClient(_FakeNotionClient):
            def create_database_page(self, database_id, properties, blocks):
                raise NotionError("page update failed")

        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model,
            client=FailingClient())
        self.assertFalse(result["ok"])
        from livingpc.curiosity_metrics import MetricStore
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            snapshot = metrics.latest_snapshot(self.cid)
            self.assertIsNone(snapshot.notion_chart_upload_id)
            self.assertIsNone(snapshot.chart_digest)
        finally:
            metrics.close()

    def test_database_mode_preserves_legacy_page_and_remaps_to_new_row(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_curiosity_database_id = "database-123"
        self._calibrate_and_publish()
        self.store.set_notion_page_id(self.cid, "legacy-child-page")
        client = _FakeNotionClient()
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model, client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(client.replaced, [])
        self.assertEqual(len(client.database_created), 1)
        self.assertEqual(self.store.get_curiosity(self.cid)["notion_page_id"],
                         "generated-page-id")

    def test_missing_curiosity_returns_ok_false_without_raising(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_parent_page_id = "parent-123"
        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, 999, self.model,
            client=_FakeNotionClient())
        self.assertFalse(result["ok"])

    def test_client_exception_is_caught_and_reported(self):
        self.cfg.notion_api_key = "secret"
        self.cfg.notion_parent_page_id = "parent-123"

        class _BoomClient:
            def create_page(self, *a, **k):
                raise RuntimeError("network down")

        result = sync_curiosity_to_notion(
            self.cfg, self.mem, self.inf, self.store, self.cid, self.model,
            client=_BoomClient())
        self.assertFalse(result["ok"])
        self.assertIn("network down", result["message"])


if __name__ == "__main__":
    unittest.main()
