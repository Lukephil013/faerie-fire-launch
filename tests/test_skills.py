"""Skills framework + reminders — pure logic, offline."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LIVINGPC_AUTO_ENCRYPTION", "0")

from livingpc import skills  # noqa: E402
from livingpc.config import Config  # noqa: E402
from livingpc.reminders import ReminderStore, parse_when  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_cfg(tmp: str, **overrides) -> Config:
    cfg = Config()
    cfg.skills_dir = os.path.join(tmp, "skills")
    cfg.projects_dir = os.path.join(tmp, "projects")
    cfg.filing_backend = "stub"
    cfg.db_path = os.path.join(tmp, "e.db")
    cfg.memory_db_path = os.path.join(tmp, "m.db")
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def write_skill(cfg, name: str, code: str) -> str:
    os.makedirs(cfg.skills_dir, exist_ok=True)
    path = os.path.join(cfg.skills_dir, f"{name}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    return path


GOOD_PY = '''
SKILL = {"command": "double", "description": "double a number", "kind": "python"}
def run(args, ctx):
    return str(int(args) * 2)
'''
GOOD_PROMPT = '''
SKILL = {"command": "eli5", "description": "explain simply", "kind": "prompt",
         "system": "Explain like the user is five."}
'''
BROKEN = "this is not python at all {{{"


class TestParseWhen(unittest.TestCase):
    NOW = datetime(2026, 7, 8, 12, 0, 0)

    def test_relative(self):
        due, msg = parse_when("in 20m stretch", now=self.NOW)
        self.assertEqual(due, self.NOW + timedelta(minutes=20))
        self.assertEqual(msg, "stretch")
        due, _ = parse_when("in 1h30m x", now=self.NOW)
        self.assertEqual(due, self.NOW + timedelta(hours=1, minutes=30))

    def test_at_clock(self):
        due, msg = parse_when("at 5pm take out trash", now=self.NOW)
        self.assertEqual((due.hour, due.minute, due.day), (17, 0, 8))
        self.assertEqual(msg, "take out trash")
        due, _ = parse_when("at 9am early", now=self.NOW)   # already past noon
        self.assertEqual(due.day, 9)                         # rolls to tomorrow
        due, _ = parse_when("at 17:30 x", now=self.NOW)
        self.assertEqual((due.hour, due.minute), (17, 30))

    def test_tomorrow(self):
        due, msg = parse_when("tomorrow 9am call the bank", now=self.NOW)
        self.assertEqual((due.day, due.hour), (9, 9))
        self.assertEqual(msg, "call the bank")

    def test_unparseable(self):
        due, msg = parse_when("whenever, honestly", now=self.NOW)
        self.assertIsNone(due)


class TestReminderStore(unittest.TestCase):
    def test_add_due_fire_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReminderStore(os.path.join(tmp, "m.db"))
            now = datetime(2026, 7, 8, 12, 0, 0)
            early = store.add(now - timedelta(minutes=1), "past due")
            later = store.add(now + timedelta(hours=1), "future")
            self.assertEqual(len(store.pending()), 2)
            due = store.due(now=now)
            self.assertEqual([r["id"] for r in due], [early])
            store.mark_fired(early)
            self.assertEqual(store.due(now=now), [])
            self.assertTrue(store.cancel(later))
            self.assertEqual(store.pending(), [])
            store.close()


class TestSkillLoader(unittest.TestCase):
    def test_load_and_dispatch_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_skill(cfg, "double", GOOD_PY)
            registry = skills.load_skills(cfg)
            self.assertIn("double", registry)
            out = skills.dispatch(registry["double"], "21", {"cfg": cfg})
            self.assertEqual(out, "42")

    def test_prompt_skill_uses_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_skill(cfg, "eli5", GOOD_PROMPT)
            registry = skills.load_skills(cfg)
            out = skills.dispatch(registry["eli5"], "gravity",
                                  {"llm": lambda s, u: f"LLM({s[:7]}|{u})"})
            self.assertEqual(out, "LLM(Explain|gravity)")

    def test_broken_skill_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_skill(cfg, "bad", BROKEN)
            write_skill(cfg, "double", GOOD_PY)
            registry = skills.load_skills(cfg)
            self.assertTrue(registry["bad"].error)
            self.assertEqual(skills.dispatch(registry["double"], "2", {}), "4")
            self.assertIn("broken", skills.dispatch(registry["bad"], "", {}))

    def test_crashing_skill_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_skill(cfg, "boom", '''
SKILL = {"command": "boom", "kind": "python", "description": "x"}
def run(args, ctx): raise RuntimeError("kaboom")
''')
            registry = skills.load_skills(cfg)
            out = skills.dispatch(registry["boom"], "", {})
            self.assertIn("failed", out)
            self.assertIn("kaboom", out)

    def test_reserved_command_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_skill(cfg, "sneaky", '''
SKILL = {"command": "file", "kind": "python", "description": "x"}
def run(args, ctx): return "hijacked"
''')
            registry = skills.load_skills(cfg)
            broken = [s for s in registry.values() if s.error]
            self.assertEqual(len(broken), 1)
            self.assertIn("built-in", broken[0].error)


class TestTeach(unittest.TestCase):
    def test_draft_and_install(self):
        canned = ('{"filename": "greet.py", "code": "SKILL = {\\"command\\": '
                  '\\"greet\\", \\"kind\\": \\"python\\", \\"description\\": '
                  '\\"greet\\"}\\ndef run(args, ctx):\\n    return \\"hi \\" + args\\n"}')
        draft = skills.draft_skill("a greeter", lambda s, u: canned)
        self.assertNotIn("error", draft)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            path = skills.install_skill(cfg, draft["filename"], draft["code"])
            registry = skills.load_skills(cfg)
            self.assertIn("greet", registry)
            out = skills.dispatch(registry["greet"], "luke", {})
            self.assertEqual(out, "hi luke")
            # reinstall backs up the previous version
            skills.install_skill(cfg, draft["filename"], draft["code"])
            self.assertTrue(os.path.exists(path + ".bak"))

    def test_bad_draft_reports_error(self):
        draft = skills.draft_skill("x", lambda s, u: "no json here")
        self.assertIn("error", draft)
        draft = skills.draft_skill("x", lambda s, u: '{"filename": "a.txt", "code": "x"}')
        self.assertIn("error", draft)


class TestCompanionSkills(unittest.TestCase):
    def _companion(self, tmp, **overrides):
        os.environ.pop("LIVINGPC_DB_KEY", None)
        from livingpc.companion.brain import Companion, StubChat
        return Companion(cfg=make_cfg(tmp, **overrides), chat=StubChat())

    def test_skills_list_and_custom_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            write_skill(c.cfg, "double", GOOD_PY)
            out = c.reply("/skills reload")
            self.assertIn("/double", out)
            self.assertEqual(c.reply("/double 8"), "16")
            c.close()

    def test_unknown_slash_falls_through_to_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            out = c.reply("/nosuchthing hello")
            self.assertIn("(stub)", out)   # normal chat handled it
            c.close()

    def test_teach_flow_requires_approval(self):
        canned = ('{"filename": "greet.py", "code": "SKILL = {\\"command\\": '
                  '\\"greet\\", \\"kind\\": \\"python\\", \\"description\\": '
                  '\\"greet\\"}\\ndef run(args, ctx):\\n    return \\"hi \\" + args\\n"}')
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            c.chat.reply = lambda system, messages, max_tokens=400: canned
            out = c.reply("/teach a greeter")
            self.assertIn("read it before approving", out)
            self.assertIn("greet.py", out)
            # nothing installed yet
            self.assertFalse(os.path.exists(os.path.join(c.cfg.skills_dir, "greet.py")))
            out = c.reply("/teach approve")
            self.assertIn("Installed", out)
            self.assertTrue(os.path.exists(os.path.join(c.cfg.skills_dir, "greet.py")))
            self.assertEqual(c.reply("/greet luke"), "hi luke")
            c.close()

    def test_teach_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            c.chat.reply = lambda system, messages, max_tokens=400: \
                '{"filename": "x.py", "code": "SKILL = {}\\n"}'
            c.reply("/teach something")
            out = c.reply("/teach cancel")
            self.assertIn("discarded", out.lower())
            self.assertIn("No draft", c.reply("/teach approve"))
            c.close()

    def test_prompt_mentions_installed_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            write_skill(c.cfg, "double", GOOD_PY)
            c._skill_registry(reload=True)
            self.assertIn("/double", c.system_prompt())
            c.close()


class TestBuiltinSkills(unittest.TestCase):
    """The shipped skills/ files load and their core paths work offline."""

    def _ctx_and_registry(self, tmp):
        cfg = make_cfg(tmp, skills_dir=os.path.join(ROOT, "skills"))
        registry = skills.load_skills(cfg)
        ctx = {"cfg": cfg, "memory_db": cfg.memory_db_path,
               "llm": lambda s, u: f"(llm summary of {len(u)} chars)"}
        return cfg, registry, ctx

    def test_builtins_load_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            _cfg, registry, _ctx = self._ctx_and_registry(tmp)
            for name in ("remind", "today", "briefing"):
                self.assertIn(name, registry)
                self.assertFalse(registry[name].error, registry[name].error)

    def test_remind_set_list_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            _cfg, registry, ctx = self._ctx_and_registry(tmp)
            out = skills.dispatch(registry["remind"], "in 20m stretch", ctx)
            self.assertIn("Set #1", out)
            out = skills.dispatch(registry["remind"], "list", ctx)
            self.assertIn("stretch", out)
            out = skills.dispatch(registry["remind"], "cancel 1", ctx)
            self.assertIn("Cancelled", out)
            out = skills.dispatch(registry["remind"], "nonsense time", ctx)
            self.assertIn("couldn't read the time", out)

    def test_today_no_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            _cfg, registry, ctx = self._ctx_and_registry(tmp)
            out = skills.dispatch(registry["today"], "", ctx)
            self.assertIn("No captured activity", out)
            out = skills.dispatch(registry["today"], "not-a-date", ctx)
            self.assertIn("like", out)

    def test_briefing_empty_and_with_reminder(self):
        with tempfile.TemporaryDirectory() as tmp:
            _cfg, registry, ctx = self._ctx_and_registry(tmp)
            out = skills.dispatch(registry["briefing"], "", ctx)
            self.assertIn("Nothing on the radar", out)
            skills.dispatch(registry["remind"], "in 2h water plants", ctx)
            out = skills.dispatch(registry["briefing"], "", ctx)
            self.assertIn("llm summary", out)


class TestSchedulerReminderHook(unittest.TestCase):
    def test_fire_due_marks_and_counts(self):
        from livingpc.reminders import fire_due
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, notifications_enabled=False)  # no real toast
            store = ReminderStore(cfg.memory_db_path)
            store.add(datetime(2020, 1, 1), "long overdue")
            store.close()
            self.assertEqual(fire_due(cfg), 1)
            self.assertEqual(fire_due(cfg), 0)   # not re-fired


if __name__ == "__main__":
    unittest.main()
