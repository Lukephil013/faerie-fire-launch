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


def write_workflow(cfg, name: str, md: str) -> str:
    folder = os.path.join(cfg.skills_dir, name)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "SKILL.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
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

GOOD_MD = '''---
name: focus
description: A focus ritual. Use when the user cannot start a task.
---
# Focus
Step 1. Step 2.
'''


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


class TestWorkflowLoader(unittest.TestCase):
    def test_load_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_workflow(cfg, "focus", GOOD_MD)
            registry = skills.load_skills(cfg)
            skill = registry["focus"]
            self.assertFalse(skill.error, skill.error)
            self.assertEqual(skill.kind, "workflow")
            self.assertIn("Step 1", skill.body)
            self.assertNotIn("---", skill.description)
            self.assertTrue(skill.model_invocable)

    def test_quoted_values_and_disable_model_invocation(self):
        md = ('---\nname: "style"\n'
              "description: 'Style guide: colons are fine here.'\n"
              "disable-model-invocation: true\n---\nRules.\n")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_workflow(cfg, "style", md)
            skill = skills.load_skills(cfg)["style"]
            self.assertFalse(skill.error, skill.error)
            self.assertEqual(skill.description, "Style guide: colons are fine here.")
            self.assertFalse(skill.model_invocable)

    def test_broken_folders_are_isolated(self):
        cases = {
            "nofence": "just a body, no frontmatter",
            "renamed": "---\nname: other\ndescription: d\n---\nbody",
            "nodesc": "---\nname: nodesc\n---\nbody",
            "nobody": "---\nname: nobody\ndescription: d\n---\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            for name, md in cases.items():
                write_workflow(cfg, name, md)
            write_workflow(cfg, "focus", GOOD_MD)
            write_skill(cfg, "double", GOOD_PY)
            registry = skills.load_skills(cfg)
            for name in cases:
                self.assertTrue(registry[name].error, name)
            self.assertFalse(registry["focus"].error)
            self.assertEqual(skills.dispatch(registry["double"], "2", {}), "4")

    def test_reserved_name_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_workflow(cfg, "file", "---\nname: file\ndescription: d\n---\nbody")
            self.assertIn("built-in", skills.load_skills(cfg)["file"].error)

    def test_oversized_body_is_broken_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp, workflow_body_max_chars=50)
            write_workflow(cfg, "big", "---\nname: big\ndescription: d\n---\n" + "x" * 100)
            self.assertIn("too large", skills.load_skills(cfg)["big"].error)

    def test_collision_never_shadows_py_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_skill(cfg, "double", GOOD_PY)
            write_workflow(cfg, "double",
                           "---\nname: double\ndescription: d\n---\nbody")
            registry = skills.load_skills(cfg)
            self.assertEqual(registry["double"].kind, "python")
            self.assertEqual(skills.dispatch(registry["double"], "3", {}), "6")
            colliders = [s for s in registry.values()
                         if s.error and "collides" in s.error]
            self.assertEqual(len(colliders), 1)

    def test_workflow_menu_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_workflow(cfg, "focus", GOOD_MD)
            write_workflow(cfg, "hidden",
                           "---\nname: hidden\ndescription: d\n"
                           "disable-model-invocation: true\n---\nbody")
            write_workflow(cfg, "broke", "no frontmatter")
            write_skill(cfg, "double", GOOD_PY)
            menu = skills.workflow_menu(skills.load_skills(cfg))
            self.assertIn("- focus — A focus ritual", menu)
            self.assertNotIn("hidden", menu)
            self.assertNotIn("broke", menu)
            self.assertNotIn("double", menu)

    def test_dispatch_fallback_uses_body_as_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            write_workflow(cfg, "focus", GOOD_MD)
            registry = skills.load_skills(cfg)
            out = skills.dispatch(registry["focus"], "help",
                                  {"llm": lambda s, u: f"LLM({s[:7]}|{u})"})
            self.assertEqual(out, "LLM(# Focus|help)")


class ScriptedChat:
    """Chat backend that plays back canned replies and records every call."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def reply(self, system, messages, max_tokens=400):
        self.calls.append((system, messages))
        return self.replies.pop(0) if self.replies else "(scripted: out of replies)"


LOAD_FOCUS = '<<<faerie_skill\n{"load": "focus"}\nfaerie_skill>>>'


class TestWorkflowActivation(unittest.TestCase):
    def _companion(self, tmp, chat, **overrides):
        os.environ.pop("LIVINGPC_DB_KEY", None)
        from livingpc.companion.brain import Companion
        c = Companion(cfg=make_cfg(tmp, **overrides), chat=chat)
        write_workflow(c.cfg, "focus", GOOD_MD)
        c._skill_registry(reload=True)
        return c

    def test_prompt_carries_menu_not_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp, ScriptedChat([]))
            static, dynamic = c.system_blocks()
            self.assertIn("focus — A focus ritual", static)
            self.assertIn("faerie_skill", static)
            self.assertNotIn("Step 1", static)
            self.assertNotIn("Step 1", dynamic)
            c.close()

    def test_sentinel_load_recalls_once_with_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            chat = ScriptedChat([LOAD_FOCUS, "here is the ritual"])
            c = self._companion(tmp, chat)
            out = c.reply("I can't get started today")
            self.assertEqual(out, "here is the ritual")
            self.assertEqual(len(chat.calls), 2)
            self.assertEqual(c._active_skills, ["focus"])
            self.assertIn("Step 1", chat.calls[1][0][1])      # dynamic block
            self.assertNotIn("Step 1", chat.calls[0][0][1])   # not before load
            c.close()

    def test_unknown_or_active_load_strips_without_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            chat = ScriptedChat(
                ['<<<faerie_skill\n{"load": "nope"}\nfaerie_skill>>> anyway'])
            c = self._companion(tmp, chat)
            out = c.reply("hello")
            self.assertEqual(out, "anyway")
            self.assertEqual(len(chat.calls), 1)
            self.assertEqual(c._active_skills, [])
            c.close()
        with tempfile.TemporaryDirectory() as tmp:
            chat = ScriptedChat([LOAD_FOCUS + " fine"])
            c = self._companion(tmp, chat)
            c._activate_skill("focus")
            out = c.reply("hello")
            self.assertEqual(out, "fine")
            self.assertEqual(len(chat.calls), 1)
            c.close()

    def test_recall_load_activates_but_never_third_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            chat = ScriptedChat(
                [LOAD_FOCUS,
                 'ok <<<faerie_skill\n{"load": "other"}\nfaerie_skill>>>'])
            c = self._companion(tmp, chat)
            write_workflow(c.cfg, "other",
                           "---\nname: other\ndescription: d. Use when x.\n---\nOther body")
            c._skill_registry(reload=True)
            out = c.reply("hm")
            self.assertEqual(out, "ok")
            self.assertEqual(len(chat.calls), 2)
            self.assertEqual(c._active_skills, ["focus", "other"])
            c.close()

    def test_cap_evicts_oldest(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp, ScriptedChat([]), workflow_max_active=2)
            for name in ("a", "b", "c"):
                c._activate_skill(name)
            self.assertEqual(c._active_skills, ["b", "c"])
            c.close()

    def test_slash_invocation_falls_through_and_activates(self):
        from livingpc.companion.brain import StubChat
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp, StubChat())
            out = c.reply("/focus can't start")
            self.assertIn("(stub)", out)
            self.assertEqual(c._active_skills, ["focus"])
            c.close()

    def test_disable_model_invocation_hidden_but_slash_works(self):
        from livingpc.companion.brain import StubChat
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp, StubChat())
            write_workflow(c.cfg, "style",
                           "---\nname: style\ndescription: d\n"
                           "disable-model-invocation: true\n---\nStyle rules")
            c._skill_registry(reload=True)
            self.assertNotIn("style", c.system_blocks()[0].split("WORKFLOW SKILLS")[-1]
                             .split("<<<")[0])
            out = c.reply("/style")
            self.assertIn("(stub)", out)
            self.assertIn("style", c._active_skills)
            self.assertIn("Style rules", c.system_blocks()[1])
            c.close()

    def test_new_chat_and_reload_prune_active(self):
        from livingpc.companion.brain import StubChat
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp, StubChat())
            c._activate_skill("focus")
            c._activate_skill("ghost")   # not on disk
            c.reply("/skills reload")
            self.assertEqual(c._active_skills, ["focus"])
            c.new_chat()
            self.assertEqual(c._active_skills, [])
            c.close()

    def test_skills_listing_tags_workflows(self):
        from livingpc.companion.brain import StubChat
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp, StubChat())
            out = c.reply("/skills")
            self.assertIn("[workflow — loads on demand]", out)
            c._activate_skill("focus")
            out = c.reply("/skills")
            self.assertIn("[workflow — loaded in this chat]", out)
            c.close()


WORKFLOW_DRAFT = ('{"type": "workflow", "name": "weekly-review", "skill_md": '
                  '"---\\nname: weekly-review\\ndescription: A weekly review. '
                  'Use when the week ends.\\n---\\nReview steps."}')


class TestTeachWorkflow(unittest.TestCase):
    def _companion(self, tmp):
        os.environ.pop("LIVINGPC_DB_KEY", None)
        from livingpc.companion.brain import Companion, StubChat
        return Companion(cfg=make_cfg(tmp), chat=StubChat())

    def test_draft_validates_skill_md(self):
        draft = skills.draft_skill("a weekly review", lambda s, u: WORKFLOW_DRAFT)
        self.assertEqual(draft["type"], "workflow")
        self.assertEqual(draft["name"], "weekly-review")
        bad = '{"type": "workflow", "name": "x", "skill_md": "no frontmatter"}'
        self.assertIn("error", skills.draft_skill("x", lambda s, u: bad))
        bad = '{"type": "workflow", "name": "File", "skill_md": "---\\nname: file\\n---\\nb"}'
        self.assertIn("error", skills.draft_skill("x", lambda s, u: bad))

    def test_force_type_reaches_prompt(self):
        seen = {}
        def llm(system, user):
            seen["system"] = system
            return WORKFLOW_DRAFT
        skills.draft_skill("x", llm, force_type="reference")
        self.assertIn("fixed the type: reference", seen["system"])

    def test_teach_workflow_flow_requires_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._companion(tmp)
            c.chat.reply = lambda system, messages, max_tokens=400: WORKFLOW_DRAFT
            out = c.reply("/teach workflow a weekly review ritual")
            self.assertIn("workflow", out)
            self.assertIn("weekly-review", out)
            expected = os.path.join(c.cfg.skills_dir, "weekly-review", "SKILL.md")
            self.assertFalse(os.path.exists(expected))
            out = c.reply("/teach approve")
            self.assertIn("Installed", out)
            self.assertTrue(os.path.exists(expected))
            self.assertIn("weekly-review", c._skill_registry())
            c.close()

    def test_install_workflow_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            path = skills.install_workflow_skill(cfg, "weekly-review", "v1")
            skills.install_workflow_skill(cfg, "weekly-review", "v2")
            self.assertTrue(os.path.exists(path + ".bak"))
            with self.assertRaises(ValueError):
                skills.install_workflow_skill(cfg, "..", "x")


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
            for name in ("remind", "today", "briefing", "memory-review",
                         "decision-helper", "goal-decomposition",
                         "house-writing-style"):
                self.assertIn(name, registry)
                self.assertFalse(registry[name].error, registry[name].error)
            self.assertFalse(registry["house-writing-style"].model_invocable)

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
