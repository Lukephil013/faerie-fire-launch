"""Encrypted document context for Soul Calibration and Investigations."""
import os
import sqlite3
import tempfile
import zipfile

from livingpc import crypto, soul_calibration
from livingpc.companion.brain import Companion, StubChat
from livingpc.config import Config
from livingpc.context_attachment import ContextAttachmentStore, extract_document
from livingpc.curiosity import CuriosityStore, answer_item, _build_context
from livingpc.goals import GoalStore
from livingpc.inference import InferenceStore
from livingpc.memory import MemoryStore
from gui import GuiApi


class ResolveCapture:
    def __init__(self):
        self.answer = ""

    def resolve(self, directive, question, answer):
        self.answer = answer
        return {"attribute": "note", "value": answer}


def config(directory):
    return Config(db_path=os.path.join(directory, "events.db"),
                  memory_db_path=os.path.join(directory, "memory.db"),
                  inference_backend="stub", companion_backend="stub")


def test_attachment_text_and_filename_are_encrypted_at_rest(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVINGPC_DB_KEY", "context-attachment-test-key")
    monkeypatch.setenv("LIVINGPC_SALT_FILE", str(tmp_path / "salt"))
    store = ContextAttachmentStore(str(tmp_path / "memory.db"))
    try:
        saved = store.add_text("curiosity", 7, "private-journal.txt",
                               "A private journal passage about an old fear.")
        assert store.list("curiosity", 7)[0]["name"] == "private-journal.txt"
        raw = store.conn.execute(
            "SELECT filename,content_text FROM context_attachment WHERE id=?",
            (saved["id"],)).fetchone()
        assert "private-journal" not in raw["filename"]
        assert "old fear" not in raw["content_text"]
    finally:
        store.close()


def test_owner_scope_deduplication_relevant_excerpt_and_hard_remove(tmp_path):
    store = ContextAttachmentStore(str(tmp_path / "memory.db"))
    try:
        text = "Opening background.\n\nMusic practice felt joyful.\n\nWork handoffs caused an energy crash."
        first = store.add_text("curiosity", 1, "journal.md", text)
        again = store.add_text("curiosity", 1, "renamed.md", text)
        store.add_text("curiosity", 2, "other.txt", "Unrelated private context")
        assert first["id"] == again["id"] and again["deduped"]
        block = store.context_block([("curiosity", 1)], query="energy handoff", max_chars=200)
        assert "energy crash" in block and "Unrelated" not in block
        assert store.remove(first["id"], "curiosity", 1)
        assert store.list("curiosity", 1) == []
    finally:
        store.close()


def test_attachment_schema_upgrade_adds_leaf_workspace_without_losing_documents(tmp_path):
    path = tmp_path / "memory.db"
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE context_attachment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_kind TEXT NOT NULL,
            owner_key TEXT NOT NULL,
            filename TEXT NOT NULL,
            media_type TEXT NOT NULL,
            content_text TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (owner_kind IN ('soul_calibration','curiosity','curiosity_item')),
            UNIQUE (owner_kind,owner_key,content_sha256)
        )
    """)
    conn.execute(
        "INSERT INTO context_attachment "
        "(owner_kind,owner_key,filename,media_type,content_text,content_sha256,"
        "char_count,created_at) VALUES (?,?,?,?,?,?,?,?)",
        ("curiosity", "7", crypto.enc("existing.txt"), "text/plain",
         crypto.enc("Existing encrypted context."), "old-digest", 27,
         "2026-01-01T00:00:00+00:00"))
    conn.commit(); conn.close()

    store = ContextAttachmentStore(str(path))
    try:
        assert store.list("curiosity", 7)[0]["name"] == "existing.txt"
        leaf = store.add_text(
            "leaf_workspace", 42, "leaf-notes.md", "Leaf-only reference material.")
        assert leaf["owner_kind"] == "leaf_workspace"
        assert store.list("leaf_workspace", 42)[0]["name"] == "leaf-notes.md"
    finally:
        store.close()


def test_supported_text_document_is_extracted_locally(tmp_path):
    path = tmp_path / "past.md"
    path.write_text("# Earlier journal\n\nWhat I remember.", encoding="utf-8")
    extracted = extract_document(str(path))
    assert extracted["name"] == "past.md"
    assert "Earlier journal" in extracted["text"]


def test_csv_and_modern_excel_workbooks_are_extracted_locally(tmp_path):
    csv_path = tmp_path / "roles.csv"
    csv_path.write_text("Role,Years\nEngineer,5", encoding="utf-8")
    csv_document = extract_document(str(csv_path))
    assert csv_document["media_type"] == "text/csv"
    assert "Engineer,5" in csv_document["text"]

    workbook = tmp_path / "resume.xlsx"
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("xl/workbook.xml", """<?xml version="1.0"?>
          <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
            <sheets><sheet name="Experience" sheetId="1" r:id="rId1"/></sheets>
          </workbook>""")
        archive.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0"?>
          <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
            <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
          </Relationships>""")
        archive.writestr("xl/sharedStrings.xml", """<?xml version="1.0"?>
          <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <si><t>Company</t></si><si><t>Faerie Fire</t></si>
          </sst>""")
        archive.writestr("xl/worksheets/sheet1.xml", """<?xml version="1.0"?>
          <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>
            <c r="B1" t="s"><v>1</v></c></row></sheetData>
          </worksheet>""")
    excel_document = extract_document(str(workbook))
    assert "[Sheet: Experience]" in excel_document["text"]
    assert "Company\tFaerie Fire" in excel_document["text"]


def test_calibration_status_exposes_metadata_and_reset_deletes_documents(tmp_path):
    cfg = config(str(tmp_path))
    field = soul_calibration.FIELDS[0]
    key = soul_calibration.field_key(field)
    documents = ContextAttachmentStore(cfg.memory_db_path)
    documents.add_text("soul_calibration", key, "history.docx", "Past journal context")
    documents.close()
    companion = Companion(cfg=cfg, chat=StubChat())
    try:
        status = companion.calibration_status()
        first = status["sections"][0]["attributes"][0]
        assert first["attachment_key"] == key
        assert first["attachments"][0]["name"] == "history.docx"
        companion.calibration_reset()
    finally:
        companion.close()
    documents = ContextAttachmentStore(cfg.memory_db_path)
    try:
        assert documents.list("soul_calibration", key) == []
    finally:
        documents.close()


def test_investigation_and_question_documents_reach_prompts_but_not_exact_memory(tmp_path):
    cfg = config(str(tmp_path))
    mem = MemoryStore(cfg.memory_db_path)
    inf = InferenceStore(cfg.memory_db_path)
    curiosities = CuriosityStore(cfg.memory_db_path)
    documents = ContextAttachmentStore(cfg.memory_db_path)
    try:
        cid = curiosities.add_curiosity("Understand energy crashes", "Energy")
        item_id = curiosities.add_item(
            cid, "question", "What happens before the crash?", confidence=.9)
        documents.add_text("curiosity", cid, "past-journal.md", "Meals were often delayed.")
        documents.add_text("curiosity_item", item_id, "today.txt", "Today the handoff began at noon.")
        context = _build_context(mem, inf, curiosities, cid)
        assert "Meals were often delayed" in context.attachment_block
        assert "handoff began at noon" in context.attachment_block
        model = ResolveCapture()
        result = answer_item(mem, curiosities, item_id,
                             "Please see the attached documents; today I felt steadier.", model)
        assert "past-journal.md" in model.answer and "today.txt" in model.answer
        fact = mem.get(result["resulting_memory_id"])
        assert fact["value"] == "Please see the attached documents; today I felt steadier."
        assert "Meals were often delayed" not in fact["value"]
    finally:
        documents.close(); curiosities.close(); inf.close(); mem.close()


def test_gui_picker_persists_and_removes_investigation_document(tmp_path):
    cfg = config(str(tmp_path))
    curiosities = CuriosityStore(cfg.memory_db_path)
    cid = curiosities.add_curiosity("Understand transitions", "Transitions")
    curiosities.close()
    document = tmp_path / "transition-notes.txt"
    document.write_text("Handoffs feel easier after a clear ending.", encoding="utf-8")

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            return [str(document)]

    api = GuiApi(cfg=cfg)
    api._window = Window()
    added = api.context_attachment_add("curiosity", cid)
    assert added["ok"] and added["attachment"]["name"] == document.name
    state = api.curiosity_state()
    row = next(item for item in state["curiosities"] if item["id"] == cid)
    assert row["context_attachments"][0]["id"] == added["attachment"]["id"]
    removed = api.context_attachment_remove(
        added["attachment"]["id"], "curiosity", cid)
    assert removed == {"ok": True, "removed": True}


def test_gui_picker_accepts_only_real_leaf_workspace_owners(tmp_path):
    cfg = config(str(tmp_path))
    goals = GoalStore(cfg.memory_db_path)
    root = goals.create("overgoal", "Career")
    project = goals.create("subgoal", "Portfolio project", parent_id=root)
    leaf = goals.create("task", "Scan project brief", parent_id=project)
    goals.close()
    document = tmp_path / "brief.md"
    document.write_text("The brief requires a CSV export and audit trail.", encoding="utf-8")

    class Window:
        def create_file_dialog(self, *_args, **_kwargs):
            return [str(document)]

    api = GuiApi(cfg=cfg)
    api._window = Window()
    added = api.context_attachment_add("leaf_workspace", leaf)
    assert added["ok"] and added["attachment"]["name"] == "brief.md"
    rejected = api.context_attachment_add("leaf_workspace", project)
    assert rejected["ok"] is False and "Leaf Workspace owner" in rejected["message"]
    documents = ContextAttachmentStore(cfg.memory_db_path)
    try:
        assert [item["name"] for item in documents.list("leaf_workspace", leaf)] == [
            "brief.md"]
    finally:
        documents.close()
