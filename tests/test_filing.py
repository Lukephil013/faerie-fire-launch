"""Filing engine tests — pure logic, offline (stub backend, temp dirs)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc import filing  # noqa: E402
from livingpc.config import Config  # noqa: E402


def make_cfg(tmp: str, **overrides) -> Config:
    cfg = Config()
    cfg.projects_dir = os.path.join(tmp, "projects")
    cfg.filing_backend = "stub"
    cfg.filing_journal_dir = os.path.join(tmp, "filed_dumps")
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class TestParse(unittest.TestCase):
    def test_plain_json(self):
        raw = json.dumps({"filings": [{"action": "append", "project": "etsy-seo",
                                       "section_title": "pricing",
                                       "markdown": "text", "confidence": 0.9}],
                          "clarify": None})
        result = filing.parse_response(raw)
        self.assertEqual(len(result.filings), 1)
        self.assertEqual(result.filings[0].project, "etsy-seo")
        self.assertIsNone(result.clarify)

    def test_fenced_json(self):
        raw = "```json\n" + json.dumps(
            {"filings": [], "clarify": "which project?"}) + "\n```"
        result = filing.parse_response(raw)
        self.assertEqual(result.filings, [])
        self.assertEqual(result.clarify, "which project?")

    def test_garbage(self):
        result = filing.parse_response("no json here at all")
        self.assertEqual(result.filings, [])
        self.assertIsNone(result.clarify)

    def test_bad_items_skipped(self):
        raw = json.dumps({"filings": ["not-a-dict",
                                      {"action": "append", "project": "x",
                                       "markdown": "ok", "confidence": 1}]})
        result = filing.parse_response(raw)
        self.assertEqual(len(result.filings), 1)


class TestCatalog(unittest.TestCase):
    def test_read_doc_and_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = os.path.join(tmp, "projects")
            os.makedirs(pdir)
            with open(os.path.join(pdir, "etsy-seo.md"), "w", encoding="utf-8") as f:
                f.write("# Etsy SEO Automation\n\n> Automating shop SEO.\n\n"
                        "## Log\n\n### 2026-07-01 — start  <!-- ff:entry abc1 -->\n\n"
                        "kickoff notes\n")
            catalog = filing.build_catalog(pdir)
            self.assertEqual(len(catalog), 1)
            doc = catalog[0]
            self.assertEqual(doc["slug"], "etsy-seo")
            self.assertEqual(doc["title"], "Etsy SEO Automation")
            self.assertEqual(doc["summary"], "Automating shop SEO.")
            # marker comments never leak into the catalog text
            self.assertNotIn("ff:entry", filing.format_catalog(catalog))

    def test_empty_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(filing.build_catalog(os.path.join(tmp, "nope")), [])
            self.assertIn("no project docs",
                          filing.format_catalog([]))

    def test_catalog_cap(self):
        catalog = [{"slug": f"p{i}", "title": "T" * 50, "summary": "S" * 300,
                    "headings": [f"## H{j}" for j in range(12)], "chars": 1,
                    "path": ""} for i in range(50)]
        self.assertLessEqual(len(filing.format_catalog(catalog, 500)), 500)


class TestApplier(unittest.TestCase):
    def _filing(self, **kw):
        base = dict(action="create", project="Etsy SEO Automation",
                    section_title="pricing idea", markdown="charge more.",
                    summary_update="An Etsy SEO project.", confidence=0.9)
        base.update(kw)
        return filing.Filing(**base)

    def test_create_then_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 7, 8, 12, 0, 0)
            first = filing.apply_filing(tmp, self._filing(), now=now)
            self.assertTrue(first["created"])
            path = first["path"]
            self.assertTrue(os.path.basename(path), "etsy-seo-automation.md")
            second = filing.apply_filing(
                tmp, self._filing(action="append", project="etsy-seo-automation",
                                  section_title="second", markdown="more.",
                                  summary_update=None), now=now)
            self.assertFalse(second["created"])
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("# Etsy SEO Automation", text)
            self.assertIn("### 2026-07-08 — pricing idea", text)
            self.assertIn("### 2026-07-08 — second", text)
            self.assertIn(f"ff:entry {first['entry_id']}", text)
            self.assertIn(f"ff:entry {second['entry_id']}", text)

    def test_never_touches_existing_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notes.md")
            hand_written = ("# Notes\n\n> Old summary.\n\nHand-written paragraph "
                            "that must survive.\n\n## Ideas\n\n- keep me\n\n## Log\n")
            with open(path, "w", encoding="utf-8") as f:
                f.write(hand_written)
            filing.apply_filing(tmp, self._filing(
                action="append", project="notes", summary_update="New summary."),
                now=datetime(2026, 7, 8))
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("Hand-written paragraph that must survive.", text)
            self.assertIn("- keep me", text)
            self.assertIn("> New summary.", text)
            self.assertNotIn("> Old summary.", text)  # the one sanctioned edit

    def test_slug_sanitized_never_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = filing.apply_filing(tmp, self._filing(
                project="../../etc/passwd"), now=datetime(2026, 7, 8))
            self.assertEqual(os.path.dirname(result["path"]),
                             os.path.normpath(tmp))
            self.assertTrue(os.path.exists(result["path"]))

    def test_missing_log_heading_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bare.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Bare\n\njust prose\n")
            filing.apply_filing(tmp, self._filing(
                action="append", project="bare", summary_update=None),
                now=datetime(2026, 7, 8))
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("## Log", text)
            self.assertIn("just prose", text)


class TestUndo(unittest.TestCase):
    def test_undo_removes_exactly_one_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime(2026, 7, 8)
            f1 = filing.apply_filing(tmp, filing.Filing(
                action="create", project="Proj", section_title="one",
                markdown="first entry", confidence=1), now=now)
            f2 = filing.apply_filing(tmp, filing.Filing(
                action="append", project="proj", section_title="two",
                markdown="second entry", confidence=1), now=now)
            result = filing.undo(tmp, f1["entry_id"])
            self.assertTrue(result["found"])
            self.assertFalse(result["deleted_doc"])
            with open(f2["path"], encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("first entry", text)
            self.assertIn("second entry", text)

    def test_undo_sole_entry_deletes_doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            created = filing.apply_filing(tmp, filing.Filing(
                action="create", project="Solo", section_title="only",
                markdown="only entry", confidence=1), now=datetime(2026, 7, 8))
            result = filing.undo(tmp, created["entry_id"])
            self.assertTrue(result["found"])
            self.assertTrue(result["deleted_doc"])
            self.assertFalse(os.path.exists(created["path"]))

    def test_undo_unknown_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(filing.undo(tmp, "nope123")["found"])


class TestPipeline(unittest.TestCase):
    def test_end_to_end_stub(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            result = filing.file_dump(cfg, "A long thought about my new idea.\n"
                                           "It has several parts.")
            self.assertIsNone(result["clarify"])
            self.assertEqual(len(result["filed"]), 1)
            self.assertTrue(os.path.exists(result["filed"][0]["path"]))
            # stub files into inbox
            self.assertEqual(result["filed"][0]["slug"], "inbox")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            result = filing.file_dump(cfg, "dry run dump", dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertFalse(os.path.isdir(cfg.projects_dir))

    def test_empty_dump_clarifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            result = filing.file_dump(cfg, "   \n  ")
            self.assertEqual(result["filed"], [])
            self.assertTrue(result["clarify"])

    def test_low_confidence_clarifies(self):
        class TimidBackend:
            def file(self, dump, catalog_text):
                return filing.FilingResult(filings=[filing.Filing(
                    action="append", project="inbox", markdown=dump,
                    confidence=0.2)])
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            result = filing.file_dump(cfg, "ambiguous", backend=TimidBackend())
            self.assertEqual(result["filed"], [])
            self.assertTrue(result["clarify"])

    def test_secrets_redacted_before_backend(self):
        captured = {}

        class SpyBackend(filing.StubBackend):
            def file(self, dump, catalog_text):
                captured["dump"] = dump
                return super().file(dump, catalog_text)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            filing.file_dump(cfg, "email me at luke@example.com about the idea",
                             backend=SpyBackend())
            self.assertNotIn("luke@example.com", captured["dump"])

    def test_filing_to_memory_writes_journal_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, filing_to_memory=True)
            filing.file_dump(cfg, "a dump that should also reach memory",
                             now=datetime(2026, 7, 8, 9, 0, 0))
            files = os.listdir(cfg.filing_journal_dir)
            self.assertEqual(len(files), 1)
            # journal-format: parseable by the existing import path
            from livingpc.journal_import import load_journals
            journals = load_journals(cfg.filing_journal_dir)
            self.assertEqual(len(journals), 1)
            entries = journals[0]["entries"]
            self.assertEqual(entries[0]["date"], "2026-07-08")
            self.assertIn("reach memory", entries[0]["text"])


class TestDistill(unittest.TestCase):
    def test_stub_distill_no_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            filing.file_dump(cfg, "seed entry")
            result = filing.distill_project(cfg, "inbox")
            self.assertFalse(result["changed"])
            self.assertFalse(result["applied"])

    def test_apply_saves_history_copy(self):
        class RewriteBackend(filing.StubBackend):
            def distill(self, doc_text):
                return "# inbox\n\n> Reorganized.\n\n## Log\n"

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            filing.file_dump(cfg, "seed entry")
            result = filing.distill_project(cfg, "inbox",
                                            backend=RewriteBackend(),
                                            apply=True,
                                            now=datetime(2026, 7, 8, 10, 0, 0))
            self.assertTrue(result["applied"])
            history = os.listdir(os.path.join(cfg.projects_dir, ".history"))
            self.assertEqual(len(history), 1)
            with open(os.path.join(cfg.projects_dir, ".history", history[0]),
                      encoding="utf-8") as f:
                self.assertIn("seed entry", f.read())  # original preserved

    def test_missing_doc_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            with self.assertRaises(FileNotFoundError):
                filing.distill_project(cfg, "ghost")


class TestCompanionFiling(unittest.TestCase):
    """The /file, /undo, /projects commands and the auto-offer, offline."""

    def _companion(self, tmp: str, **overrides):
        os.environ.pop("LIVINGPC_DB_KEY", None)
        from livingpc.companion.brain import Companion, StubChat
        cfg = make_cfg(tmp, db_path=os.path.join(tmp, "e.db"),
                       memory_db_path=os.path.join(tmp, "m.db"), **overrides)
        return Companion(cfg=cfg, chat=StubChat())

    def test_file_command_files_and_undo_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            out = c.reply("/file a thought about my new garden project")
            self.assertIn("inbox", out.lower())
            self.assertIn("/undo", out)
            entry_id = out.rsplit("/undo ", 1)[1].split("`")[0].strip()
            out2 = c.reply(f"/undo {entry_id}")
            self.assertIn("Undone", out2)
            self.assertEqual(len(c.history), 4)  # both turns recorded
            c.close()

    def test_projects_command_lists_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            self.assertIn("No project docs yet", c.reply("/projects"))
            c.reply("/file seed one project")
            self.assertIn("inbox", c.reply("/projects"))
            c.close()

    def test_long_message_gets_no_offer_but_explicit_file_still_works(self):
        # The proactive /file nudge was removed by request: a long brain-dump
        # must NOT get an offer. Explicit filing with content still works.
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp, filing_offer_min_chars=50)
            long_dump = "A long meandering thought about the thing. " * 3
            out = c.reply(long_dump)
            self.assertNotIn("/file", out)
            self.assertNotIn("worth keeping", out)
            out2 = c.reply("/file " + long_dump)
            self.assertIn("inbox", out2.lower())
            c.close()

    def test_short_message_gets_no_offer(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp, filing_offer_min_chars=600)
            out = c.reply("hi there")
            self.assertNotIn("/file", out)
            c.close()

    def test_prompt_tells_companion_about_filing(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            prompt = c.system_prompt()
            self.assertIn("/file", prompt)
            self.assertIn("FILING", prompt)
            c.close()

    def test_clarify_keeps_dump_and_combines_followup(self):
        from unittest.mock import patch
        import livingpc.filing as filing_mod
        calls = []
        real = filing_mod.file_dump

        def fake(cfg, dump, **kw):
            calls.append(dump)
            if len(calls) == 1:
                return {"filed": [], "clarify": "Which project?",
                        "dry_run": False}
            return real(cfg, dump, **kw)

        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            with patch.object(filing_mod, "file_dump", side_effect=fake):
                out = c.reply("/file an ambiguous thought")
                self.assertIn("Which project?", out)
                out2 = c.reply("/file it is about the garden")
                self.assertIn("inbox", out2.lower())
            self.assertIn("an ambiguous thought", calls[1])
            self.assertIn("garden", calls[1])
            c.close()

    def test_filing_command_never_crashes_the_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = os.environ.pop("ANTHROPIC_API_KEY", None)
            try:
                c = self._companion(tmp, filing_backend="claude")  # key absent
                out = c.reply("/file this should degrade gracefully")
                self.assertIn("trouble filing", out)
                c.close()
            finally:
                if key is not None:
                    os.environ["ANTHROPIC_API_KEY"] = key


class TestAttachments(unittest.TestCase):
    """Files + pasted photos flowing through the companion turn."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _companion(self, tmp, **overrides):
        os.environ.pop("LIVINGPC_DB_KEY", None)
        from livingpc.companion.brain import Companion, StubChat
        cfg = make_cfg(tmp, db_path=os.path.join(tmp, "e.db"),
                       memory_db_path=os.path.join(tmp, "m.db"), **overrides)
        return Companion(cfg=cfg, chat=StubChat())

    def test_html_has_attachment_ui(self):
        path = os.path.join(self.ROOT, "livingpc", "companion", "companion.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        for needle in ('id="attachBtn"', 'id="attachRow"', "attach_file",
                       "addEventListener('paste'"):
            self.assertIn(needle, html)

    def test_image_attachment_reaches_model_as_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            out = c.reply("what is this?", attachments=[
                {"kind": "image", "media_type": "image/png", "data": "aGk="}])
            self.assertIn("saw 1 image", out)
            # history stores a placeholder, never the image bytes
            self.assertIn("[attached:", c.history[0]["content"])
            self.assertNotIn("aGk=", c.history[0]["content"])
            c.close()

    def test_text_attachment_folds_into_file_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            c.reply("/file notes from my draft", attachments=[
                {"kind": "text", "name": "draft.txt",
                 "text": "the deep attached content"}])
            doc = os.path.join(c.cfg.projects_dir, "inbox.md")
            with open(doc, encoding="utf-8") as f:
                self.assertIn("the deep attached content", f.read())
            c.close()

    def test_load_attachment_text_and_image_and_binary(self):
        import companion as companion_mod
        api = companion_mod.Api.__new__(companion_mod.Api)  # skip config load
        with tempfile.TemporaryDirectory() as tmp:
            txt = os.path.join(tmp, "note.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write("hello attachment")
            r = api._load_attachment(txt)
            self.assertTrue(r["ok"])
            self.assertEqual(r["attachment"]["kind"], "text")
            self.assertIn("hello attachment", r["attachment"]["text"])

            png = os.path.join(tmp, "pic.png")
            with open(png, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nfakedata")
            r = api._load_attachment(png)
            self.assertTrue(r["ok"])
            self.assertEqual(r["attachment"]["kind"], "image")
            self.assertEqual(r["attachment"]["media_type"], "image/png")

            binary = os.path.join(tmp, "app.exe")
            with open(binary, "wb") as f:
                f.write(b"MZ\x00\x00\x00garbage")
            r = api._load_attachment(binary)
            self.assertFalse(r["ok"])


class TestProjectsTab(unittest.TestCase):
    """The companion's right-side Projects tab (UI hooks + Api methods)."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_html_has_projects_tab(self):
        path = os.path.join(self.ROOT, "livingpc", "companion", "companion.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        for needle in ('id="filesPane"', 'id="filesToggle"', 'id="filesList"',
                       'id="fileView"', 'id="filesOpenFolder"',
                       "list_projects", "read_project", "open_projects_folder"):
            self.assertIn(needle, html)

    def _api(self, tmp):
        os.environ.pop("LIVINGPC_DB_KEY", None)
        import companion as companion_mod
        api = companion_mod.Api()
        api.cfg = make_cfg(tmp)
        return api

    def test_api_lists_and_reads_project_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = self._api(tmp)
            self.assertEqual(api.list_projects(), {"ok": True, "docs": []})
            from livingpc import filing
            filing.file_dump(api.cfg, "a filed thought for the tab")
            docs = api.list_projects()["docs"]
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0]["slug"], "inbox")
            doc = api.read_project("inbox")
            self.assertTrue(doc["ok"])
            self.assertIn("a filed thought for the tab", doc["text"])

    def test_api_read_missing_doc_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = self._api(tmp)
            result = api.read_project("ghost")
            self.assertFalse(result["ok"])
            self.assertIn("couldn't read", result["text"])

    def test_api_read_never_escapes_projects_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = self._api(tmp)
            result = api.read_project("../../config")
            self.assertFalse(result["ok"])  # sanitized to a flat slug, missing


class TestSnapshot(unittest.TestCase):
    def test_snapshot_and_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = os.path.join(tmp, "projects")
            os.makedirs(pdir)
            with open(os.path.join(pdir, "a.md"), "w", encoding="utf-8") as f:
                f.write("# A\n")
            bdir = os.path.join(tmp, "backups")
            for i in range(3):
                filing.snapshot_projects(pdir, bdir, keep=2,
                                         now=datetime(2026, 7, 8, 10, 0, i))
            snaps = [n for n in os.listdir(bdir) if n.startswith("projects-")]
            self.assertEqual(len(snaps), 2)

    def test_snapshot_skips_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = filing.snapshot_projects(os.path.join(tmp, "none"),
                                              os.path.join(tmp, "backups"))
            self.assertEqual(result["path"], "")


if __name__ == "__main__":
    unittest.main()
