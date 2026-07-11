"""Notion sync — mirrors each curiosity to a page in Notion.

The legacy mode creates child pages under a configured parent and rewrites the
whole page. The optional database mode creates rows in a Curiosities database
and rewrites only a clearly marked toggle, preserving user-authored content.

Best-effort by design, same posture as notify.py: a missing token, an
unreachable network, or a Notion API error is logged and swallowed, never
allowed to interrupt the curiosity flow itself. See sync_curiosity_to_notion,
called from gui.py's curiosity bridge methods.

Uses only the standard library (urllib) — no new dependency for one HTTP
integration used from a handful of call sites.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .diagnostics import log_diag

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
NOTION_FILE_VERSION = NOTION_VERSION
_MAX_CHILDREN_PER_CALL = 100  # Notion's per-request cap on block children
_MANAGED_TOGGLE_TITLE = "Living Computer — Synced"


class NotionError(RuntimeError):
    pass


def _commit_store_connections(*owners) -> None:
    """Release SQLite write locks before leaving the process for HTTP work."""
    for owner in owners:
        conn = getattr(owner, "conn", None)
        if conn is not None:
            conn.commit()


class NotionClient:
    """Thin wrapper over the handful of Notion API calls this feature needs."""

    def __init__(self, token: str, timeout_seconds: float = 20.0):
        self.token = token
        self.timeout = timeout_seconds

    def _request(self, method: str, path: str, body: dict | None = None,
                 *, version: str | None = None) -> dict:
        url = f"{NOTION_API_BASE}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": version or NOTION_VERSION,
            "Content-Type": "application/json",
        }, version=NOTION_FILE_VERSION)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise NotionError(f"Notion API {error.code}: {detail[:300]}") from error
        except urllib.error.URLError as error:
            raise NotionError(f"Notion API unreachable: {error.reason}") from error

    def create_page(self, parent_page_id: str, title: str, blocks: list[dict]) -> str:
        result = self._request("POST", "/pages", {
            "parent": {"page_id": parent_page_id},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
            "children": blocks[:_MAX_CHILDREN_PER_CALL],
        })
        page_id = result.get("id")
        if not page_id:
            raise NotionError(f"Notion create_page returned no id: {result}")
        if len(blocks) > _MAX_CHILDREN_PER_CALL:
            self._append_children(page_id, blocks[_MAX_CHILDREN_PER_CALL:])
        return page_id

    def retrieve_page(self, page_id: str) -> dict:
        """Read-only reachability/permission check: does this token have
        access to this page? Raises NotionError if not (bad token, or the
        page hasn't been shared with the integration). See tools/check_notion.py."""
        return self._request("GET", f"/pages/{page_id}")

    def page_belongs_to_database(self, page_id: str, database_id: str) -> bool:
        page = self.retrieve_page(page_id)
        parent = page.get("parent", {})
        actual_id = str(parent.get(
            "data_source_id", parent.get("database_id", ""))).replace("-", "")
        expected_id = str(database_id).replace("-", "")
        return parent.get("type") in {"data_source_id", "database_id"} and actual_id == expected_id

    def retrieve_database(self, database_id: str) -> dict:
        return self._request("GET", f"/databases/{database_id}")

    def resolve_data_source(self, database_id: str, configured_id: str = "") -> str:
        if configured_id:
            return configured_id
        sources = self.retrieve_database(database_id).get("data_sources", [])
        if len(sources) != 1:
            raise NotionError(
                "Set notion_curiosity_data_source_id when the database has zero or multiple data sources")
        return str(sources[0]["id"])

    def ensure_metric_properties(self, data_source_id: str) -> None:
        source = self._request("GET", f"/data_sources/{data_source_id}")
        existing = source.get("properties", {})
        required = {
            "Level": {"number": {}}, "XP": {"number": {}},
            "Mastery": {"number": {}}, "Metric Confidence": {"number": {}},
            "7-Day Trend": {"number": {}}, "Last Snapshot": {"date": {}},
        }
        missing = {name: value for name, value in required.items() if name not in existing}
        if missing:
            self._request("PATCH", f"/data_sources/{data_source_id}",
                          {"properties": missing})

    def set_page_cover(self, page_id: str, file_upload_id: str) -> None:
        """Attach a reusable Notion-hosted cover using the current Files API."""
        self._request("PATCH", f"/pages/{page_id}", {
            "cover": {
                "type": "file_upload",
                "file_upload": {"id": file_upload_id},
            },
        }, version=NOTION_FILE_VERSION)

    def upload_file(self, path: str) -> str:
        """Upload a private generated chart and return its reusable upload id."""
        filename = os.path.basename(path)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        created = self._request("POST", "/file_uploads", {
            "mode": "single_part", "filename": filename, "content_type": content_type,
        }, version=NOTION_FILE_VERSION)
        upload_id = created.get("id")
        if not upload_id:
            raise NotionError("Notion file upload returned no id")
        boundary = "----LivingComputer" + uuid.uuid4().hex
        with open(path, "rb") as handle:
            payload = handle.read()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode(
                    "utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("ascii")
        req = urllib.request.Request(
            f"{NOTION_API_BASE}/file_uploads/{upload_id}/send", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Notion-Version": NOTION_FILE_VERSION,
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            raise NotionError(f"Notion file upload failed: {type(error).__name__}") from error
        return str(upload_id)

    def replace_page_content(self, page_id: str, title: str, blocks: list[dict]) -> None:
        self._request("PATCH", f"/pages/{page_id}", {
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
        })
        self._clear_children(page_id)
        self._append_children(page_id, blocks)

    def create_database_page(self, database_id: str, properties: dict,
                             blocks: list[dict]) -> str:
        toggle = _managed_toggle(blocks[:_MAX_CHILDREN_PER_CALL])
        result = self._request("POST", "/pages", {
            "parent": {"type": "data_source_id", "data_source_id": database_id},
            "properties": properties,
            "icon": {"type": "emoji", "emoji": "🌱"},
            "children": [toggle],
        })
        page_id = result.get("id")
        if not page_id:
            raise NotionError(f"Notion create_database_page returned no id: {result}")
        if len(blocks) > _MAX_CHILDREN_PER_CALL:
            managed_id = self._find_managed_toggle(page_id)
            if not managed_id:
                raise NotionError("created database page has no managed toggle")
            self._append_children(managed_id, blocks[_MAX_CHILDREN_PER_CALL:])
        return page_id

    def update_database_page(self, page_id: str, properties: dict,
                             blocks: list[dict]) -> None:
        self._request("PATCH", f"/pages/{page_id}", {"properties": properties})
        managed_id = self._find_managed_toggle(page_id)
        if managed_id:
            self._clear_children(managed_id)
            self._append_children(managed_id, blocks)
        else:
            self._append_children(page_id, [_managed_toggle(blocks[:_MAX_CHILDREN_PER_CALL])])
            if len(blocks) > _MAX_CHILDREN_PER_CALL:
                managed_id = self._find_managed_toggle(page_id)
                if not managed_id:
                    raise NotionError("could not recreate managed toggle")
                self._append_children(managed_id, blocks[_MAX_CHILDREN_PER_CALL:])

    def _find_managed_toggle(self, page_id: str) -> str | None:
        cursor = None
        while True:
            path = f"/blocks/{page_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            result = self._request("GET", path)
            for block in result.get("results", []):
                if block.get("type") != "toggle":
                    continue
                rich_text = block.get("toggle", {}).get("rich_text", [])
                title = "".join(item.get("plain_text", "") or
                                item.get("text", {}).get("content", "")
                                for item in rich_text)
                if title == _MANAGED_TOGGLE_TITLE:
                    return block.get("id")
            if not result.get("has_more"):
                return None
            cursor = result.get("next_cursor")

    def _clear_children(self, page_id: str) -> None:
        cursor = None
        while True:
            path = f"/blocks/{page_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            result = self._request("GET", path)
            for block in result.get("results", []):
                self._request("DELETE", f"/blocks/{block['id']}")
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")

    def _append_children(self, page_id: str, blocks: list[dict]) -> None:
        for i in range(0, len(blocks), _MAX_CHILDREN_PER_CALL):
            chunk = blocks[i:i + _MAX_CHILDREN_PER_CALL]
            self._request("PATCH", f"/blocks/{page_id}/children", {"children": chunk})


def _text_block(kind: str, text: str) -> dict:
    return {"object": "block", "type": kind,
            kind: {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def _managed_toggle(blocks: list[dict]) -> dict:
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{"type": "text", "text": {"content": _MANAGED_TOGGLE_TITLE}}],
            "color": "green_background",
            "children": blocks,
        },
    }


def curiosity_database_properties(curiosity: dict, *, synced_at: str | None = None,
                                  snapshot=None) -> dict:
    """Translate one local curiosity into the fixed Life Hub database schema."""
    status = str(curiosity.get("status", "active")).strip().capitalize()
    if status not in {"Active", "Paused", "Archived"}:
        status = "Active"
    synced_at = synced_at or datetime.now(timezone.utc).isoformat()
    properties = {
        "Name": {"title": [{"text": {"content": str(curiosity["label"])[:2000]}}]},
        "Status": {"select": {"name": status}},
        "Focus": {"select": {"name": "Greatest" if curiosity.get("is_greatest") else
                                           "Background"}},
        "Last Synced": {"date": {"start": synced_at}},
        "Local Curiosity ID": {"number": int(curiosity["id"])},
    }
    if snapshot is not None:
        properties.update({
            "Level": {"number": snapshot.level},
            "XP": {"number": snapshot.total_xp},
            "Mastery": {"number": snapshot.overall_mastery},
            "Metric Confidence": {"number": round(snapshot.overall_confidence * 100, 2)},
            "7-Day Trend": {"number": snapshot.trend_7d},
            "Last Snapshot": {"date": {"start": snapshot.snapshot_date}},
        })
    return properties


def metric_dashboard_blocks(profile, snapshot, history, chart_upload_id=None) -> list[dict]:
    """Privacy-safe dashboard content for the managed Notion toggle."""
    mastery = "unknown" if snapshot.overall_mastery is None else f"{snapshot.overall_mastery:.0f}%"
    trend = "unknown" if snapshot.trend_7d is None else f"{snapshot.trend_7d:+.1f} points"
    blocks = [
        _text_block("heading_2", f"Level {snapshot.level} · {snapshot.total_xp} XP"),
        _text_block("paragraph", f"Next level: {snapshot.xp_into_level}/100 XP"),
        _text_block("paragraph", f"Mastery {mastery} · confidence "
                    f"{snapshot.overall_confidence * 100:.0f}% · 7-day trend {trend}"),
        _text_block("heading_3", "Current state"),
    ]
    def progress(value):
        if value is None:
            return "unknown"
        filled = max(0, min(10, round(float(value) / 10)))
        return "█" * filled + "░" * (10 - filled) + f" {float(value):.0f}/100"

    for item in profile.state_metrics:
        value = snapshot.state.get(item.slug)
        blocks.append(_text_block("bulleted_list_item", f"{item.label}: "
                                  f"{progress(value)}"))
    blocks.append(_text_block("heading_3", "Growth dimensions"))
    for item in profile.dimensions:
        metric = snapshot.metrics.get(item.slug, {})
        value, confidence = metric.get("mastery"), metric.get("confidence")
        blocks.append(_text_block(
            "bulleted_list_item", f"{item.label}: {progress(value)} · "
            f"confidence {float(confidence or 0) * 100:.0f}%"))
    if chart_upload_id:
        blocks.append({"object": "block", "type": "image", "image": {
            "type": "file_upload", "file_upload": {"id": chart_upload_id}}})
    populated = [(slug, values.get("mastery")) for slug, values in snapshot.metrics.items()
                 if values.get("mastery") is not None]
    next_slug = min(populated, key=lambda pair: pair[1])[0] if populated else None
    next_dimension = next((d for d in profile.dimensions if d.slug == next_slug), None)
    previous = next((item for item in reversed(history)
                     if item.snapshot_date < snapshot.snapshot_date and
                     item.overall_mastery is not None), None)
    changed = ("No new growth evidence today." if snapshot.evidence_count == 0 else
               (f"Mastery changed {snapshot.overall_mastery - previous.overall_mastery:+.1f} points."
                if previous and snapshot.overall_mastery is not None else
                "Today established a new evidence-based baseline."))
    blocks.extend([
        _text_block("heading_3", "What changed today"),
        _text_block("paragraph", changed + " " + snapshot.summary),
        _text_block("heading_3", "Recommended next action"),
        _text_block("paragraph", next_dimension.checkin_prompt if next_dimension else
                    "Complete a brief check-in when you have real evidence to add."),
        _text_block("heading_3", "Last seven days"),
    ])
    for item in history[-7:]:
        blocks.append(_text_block("bulleted_list_item",
                                  f"{item.snapshot_date}: {item.summary}"))
    return blocks


def curiosity_cover_file_upload_id(config, curiosity_id: int) -> str | None:
    """Choose a stable cover for a curiosity without storing extra row state."""
    cover_ids = list(getattr(config, "notion_curiosity_cover_file_upload_ids", []) or [])
    if not cover_ids:
        return None
    return cover_ids[(int(curiosity_id) - 1) % len(cover_ids)]


def markdown_to_blocks(markdown: str) -> list[dict]:
    """A small, deliberately literal markdown-to-Notion-blocks conversion —
    just enough structure for a consolidated-essentials summary: headings,
    bullets, and paragraphs. Bold (**text**) is left as literal asterisks
    rather than parsed into rich-text annotations; good enough for this use."""
    blocks: list[dict] = []
    for raw_line in (markdown or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("### "):
            blocks.append(_text_block("heading_3", line[4:].strip()))
        elif line.startswith("## "):
            blocks.append(_text_block("heading_2", line[3:].strip()))
        elif line.startswith("# "):
            blocks.append(_text_block("heading_1", line[2:].strip()))
        elif re.match(r"^[-*]\s+", line):
            blocks.append(_text_block("bulleted_list_item", re.sub(r"^[-*]\s+", "", line)))
        else:
            blocks.append(_text_block("paragraph", line.strip()))
    return blocks or [_text_block("paragraph", "(nothing yet)")]


def sync_curiosity_to_notion(config, mem, inf, store, curiosity_id: int, model,
                             *, client: NotionClient | None = None) -> dict:
    """Create (once) or rewrite the Notion child page for this curiosity.
    Best-effort: never raises. Returns {'ok': False, 'message': ...} when
    Notion isn't configured or the call fails, {'ok': True, 'page_id': ...}
    on success — callers treat this as fire-and-forget."""
    import os
    from .curiosity import notion_summary_markdown

    if not getattr(config, "notion_sync_enabled", True):
        return {"ok": False, "message": "Notion sync disabled"}
    token = getattr(config, "notion_api_key", "") or os.environ.get("NOTION_API_KEY", "")
    parent_id = getattr(config, "notion_parent_page_id", "")
    database_id = getattr(config, "notion_curiosity_database_id", "")
    if not token or not (parent_id or database_id):
        return {"ok": False, "message": "Notion not configured (token/destination missing)"}

    curiosity = store.get_curiosity(curiosity_id)
    if curiosity is None:
        return {"ok": False, "message": f"curiosity {curiosity_id} not found"}

    metrics = None
    fresh_chart = None
    try:
        from .curiosity_metrics import MetricStore, render_dashboard, snapshot_digest
        metrics = MetricStore(config.memory_db_path)
        profile = ((metrics.get_profile(curiosity_id) or metrics.ensure_profile(curiosity))
                   if getattr(config, "curiosity_metrics_enabled", True) else None)
        if (database_id and profile and profile.status == "approved" and
                profile.publication_status != "published"):
            return {"ok": True, "skipped": True,
                    "message": "Metric dashboard remains private until explicitly published"}
        if profile and profile.publication_status == "published" and not database_id:
            return {"ok": False,
                    "message": "A Curiosities database is required to publish metric dashboards"}
        if database_id and profile and profile.status == "draft":
            return {"ok": True, "skipped": True,
                    "message": "Metric profile remains private until approved and calibrated"}

        markdown = notion_summary_markdown(mem, inf, store, curiosity_id, model)
        blocks = markdown_to_blocks(markdown)
        active_client = client or NotionClient(token)
        title = curiosity["label"]
        existing_page_id = curiosity.get("notion_page_id")
        metric_snapshot = None
        _commit_store_connections(mem, inf, store, metrics)
        if database_id:
            data_source_id = active_client.resolve_data_source(
                database_id, getattr(config, "notion_curiosity_data_source_id", ""))
            candidate = metrics.latest_snapshot(curiosity_id)
            required_days = int(getattr(config, "curiosity_calibration_days", 7))
            if (profile and profile.publication_status == "published" and candidate and
                    candidate.calibration_days >= required_days):
                active_client.ensure_metric_properties(data_source_id)
                metric_snapshot = candidate
                history = metrics.history(
                    curiosity_id, int(getattr(config, "curiosity_chart_days", 30)))
                digest = snapshot_digest(profile, candidate, history)
                upload_id = (candidate.notion_chart_upload_id
                             if candidate.chart_digest == digest else None)
                if not upload_id:
                    chart_dir = os.path.join(
                        os.path.dirname(config.memory_db_path), "notion_charts")
                    chart_path, digest = render_dashboard(
                        profile, candidate, history, chart_dir)
                    upload_id = active_client.upload_file(chart_path)
                    fresh_chart = (candidate.snapshot_date, digest, upload_id)
                blocks = metric_dashboard_blocks(
                    profile, candidate, history, upload_id) + blocks
            properties = curiosity_database_properties(
                curiosity, snapshot=metric_snapshot)
            cover_id = curiosity_cover_file_upload_id(config, curiosity_id)
            if (existing_page_id and active_client.page_belongs_to_database(
                    existing_page_id, data_source_id)):
                active_client.update_database_page(existing_page_id, properties, blocks)
                page_id = existing_page_id
            else:
                page_id = active_client.create_database_page(data_source_id, properties, blocks)
                store.set_notion_page_id(curiosity_id, page_id)
            if cover_id:
                active_client.set_page_cover(page_id, cover_id)
        elif existing_page_id:
            active_client.replace_page_content(existing_page_id, title, blocks)
            page_id = existing_page_id
        else:
            page_id = active_client.create_page(parent_id, title, blocks)
            store.set_notion_page_id(curiosity_id, page_id)
        if metric_snapshot:
            if fresh_chart:
                metrics.set_chart_upload(
                    curiosity_id, fresh_chart[0], fresh_chart[1], fresh_chart[2])
            metrics.mark_published_success(curiosity_id)
        log_diag("notion", f"synced curiosity_id={curiosity_id} page_id={page_id}")
        return {"ok": True, "page_id": page_id}
    except Exception as error:
        log_diag("notion", f"sync failed curiosity_id={curiosity_id} "
                 f"error={type(error).__name__}: {error}")
        return {"ok": False, "message": f"{type(error).__name__}: {error}"}
    finally:
        if metrics is not None:
            metrics.close()


def export_goal_to_notion(config, store, goal_id: int, *,
                          client: NotionClient | None = None) -> dict:
    """Explicit one-shot export of a selected local goal subtree.

    Goal notes and evidence labels remain local. This never stores a page ID and
    is never called by the scheduler, so export cannot silently become sync.
    """
    from .triage.redact import redact

    if not getattr(config, "notion_sync_enabled", True):
        return {"ok": False, "message": "Notion sync disabled"}
    token = getattr(config, "notion_api_key", "") or os.environ.get("NOTION_API_KEY", "")
    parent_id = getattr(config, "notion_parent_page_id", "")
    if not token or not parent_id:
        return {"ok": False, "message": "Notion not configured (token/parent missing)"}

    def find(node):
        if not node:
            return None
        if int(node["id"]) == int(goal_id):
            return node
        for child in node.get("children", []):
            found = find(child)
            if found:
                return found
        return None

    node = find(store.tree())
    if not node or node.get("status") == "archived":
        return {"ok": False, "message": "active goal not found"}
    blocks = []

    def append_goal(item, depth=0):
        if item.get("status") == "archived":
            return
        kind = "heading_2" if depth == 0 else "heading_3" if depth == 1 else "bulleted_list_item"
        marker = "✓ " if item.get("status") == "completed" else ""
        blocks.append(_text_block(kind, redact(marker + str(item.get("title", "")))))
        description = redact(str(item.get("description") or ""))
        if description:
            blocks.append(_text_block("paragraph", description))
        meta = [str(item.get("type", "goal")), str(item.get("status", "active"))]
        if item.get("due_date"):
            meta.append("due " + str(item["due_date"]))
        completion = item.get("completion") or {}
        if completion.get("percent") is not None:
            meta.append(f"completion {completion['percent']:.0f}%")
        blocks.append(_text_block("paragraph", " · ".join(meta)))
        mastery = item.get("mastery")
        if mastery:
            for dimension in mastery.get("dimensions", []):
                score = mastery.get("scores", {}).get(dimension["slug"], {})
                value = score.get("mastery")
                confidence = score.get("confidence", 0)
                text = (f"{dimension['label']}: unknown" if value is None else
                        f"{dimension['label']}: {value:.0f}% mastery · "
                        f"{confidence * 100:.0f}% confidence")
                blocks.append(_text_block("bulleted_list_item", redact(text)))
        for child in item.get("children", []):
            append_goal(child, depth + 1)

    append_goal(node)
    try:
        _commit_store_connections(store)
        page_id = (client or NotionClient(token)).create_page(
            parent_id, redact(str(node["title"])), blocks)
        log_diag("notion", f"explicit goal export goal_id={goal_id} page_id={page_id}")
        return {"ok": True, "page_id": page_id}
    except Exception as error:
        log_diag("notion", f"goal export failed goal_id={goal_id} error={type(error).__name__}")
        return {"ok": False, "message": f"{type(error).__name__}: {error}"}
