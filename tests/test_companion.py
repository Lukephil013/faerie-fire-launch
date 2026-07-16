"""Tests for the companion's persona system and context-aware prompt building."""
import base64
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LIVINGPC_DB_KEY", None)

from livingpc.config import Config  # noqa: E402
from livingpc.storage import EventLog  # noqa: E402
from livingpc.memory import MemoryStore  # noqa: E402
from livingpc.companion import personas  # noqa: E402
from livingpc.companion.brain import Companion, StubChat  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class ScriptedChat:
    def __init__(self, *replies):
        self.replies = list(replies)

    def reply(self, system, messages, max_tokens=400):
        return self.replies.pop(0)


class CapturingChat:
    def __init__(self):
        self.calls = []

    def reply(self, system, messages, max_tokens=400):
        self.calls.append(messages)
        return "Okay."


class ScriptedScout:
    def __init__(self, *results):
        self.results = list(results)
        self.contexts = []

    def review(self, context):
        self.contexts.append(context)
        return self.results.pop(0) if self.results else {
            "decision": "none", "reason": "", "question": "", "proposals": []}


def test_personas_exist():
    keys = [p["key"] for p in personas.list_personas()]
    assert "companion" in keys and "coach" in keys and "gremlin" in keys
    assert personas.get_persona("gremlin").name == "Gremlin"
    assert personas.get_persona("nope").key == "companion"   # fallback


def test_companion_prompt_uses_memory_and_screen():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        # seed memory + a screen event
        mem = MemoryStore(cfg.memory_db_path)
        mem.add("League of Legends", "champion pool", "Caitlyn, Tristana")
        mem.close()
        ev = EventLog(cfg.db_path)
        ev.log_event("ocr", app="LeagueClient.exe", window_title="in-game",
                     text_payload="Baron is up in 30 seconds")
        ev.close()

        c = Companion(cfg=cfg, persona_key="coach", chat=StubChat())
        sysp = c.system_prompt()
        assert "Caitlyn, Tristana" in sysp           # knows them
        assert "Baron is up" in sysp                  # sees the screen
        assert "coach" in sysp.lower()                # persona flavor present

        out = c.reply("what should I do?")
        assert out.startswith("(stub)")
        assert len(c.history) == 2                     # user + assistant recorded
        c.close()


def test_text_attachment_remains_available_to_follow_up_turns():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        chat = CapturingChat()
        companion = Companion(cfg=cfg, chat=chat)
        companion.reply("Please read my resume.", attachments=[{
            "kind": "text", "name": "resume.docx",
            "text": "Built deployment automation and database tooling.",
        }])
        companion.reply("What did my resume say?")

        prior_user_turn = chat.calls[1][0]["content"]
        assert "Built deployment automation" in prior_user_turn
        assert "ATTACHED_DOCUMENT_CONTEXT" in prior_user_turn
        stored = companion.chats.messages(companion.chat_id)[0]["content"]
        assert "Built deployment automation" in stored
        companion.close()


def test_dropped_document_uses_native_attachment_extraction_and_original_name():
    import companion
    api = companion.Api()
    payload = base64.b64encode(
        b"A dropped project note with concrete context.").decode("ascii")

    result = api.attach_dropped_file("project-note.md", "text/markdown", payload)

    assert result["ok"] is True
    assert result["attachment"]["kind"] == "text"
    assert result["attachment"]["name"] == "project-note.md"
    assert "dropped project note" in result["attachment"]["text"]


def test_dropped_file_transfer_is_bounded_before_extraction():
    import companion
    api = companion.Api()
    api._MAX_DROP_BYTES = 3
    payload = base64.b64encode(b"four").decode("ascii")

    result = api.attach_dropped_file("too-large.txt", "text/plain", payload)

    assert result["ok"] is False
    assert "too large" in result["message"]


def test_companion_prompt_has_read_only_lifecycle_context():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        prompt = c.system_prompt()
        assert "read-only architecture reference" in prompt
        assert "Cultivation Lifecycle" in prompt
        c.close()


def test_companion_proactively_scouts_patterns_and_can_offer_several_investigations():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        prompt = c.system_prompt()
        assert "PATTERN SCOUTING IS PART OF ORDINARY CONVERSATION" in prompt
        assert "without waiting for them to ask" in prompt
        assert "at most three distinct items" in prompt
        assert "separate start_investigation block for each" in prompt
        c.close()


def test_proposal_scout_gate_is_behavioral_not_a_fixed_topic_vocabulary():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        scout = ScriptedScout(
            {"decision": "none", "reason": "", "question": "", "proposals": []})
        companion = Companion(
            cfg=cfg, chat=ScriptedChat("Grammar explanation.", "Nice progress."),
            proposal_scout=scout)

        companion.reply("Can you explain Korean grammar?")
        assert scout.contexts == []

        companion.reply("I started studying Korean grammar again.")
        assert len(scout.contexts) == 1
        assert scout.contexts[0]["signals"]["action"] is True
        companion.close()


def test_proposal_scout_uses_the_same_gate_across_unrelated_growth_domains():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        goals.create("overgoal", "Health & Energy", description="Exercise, sleep, and health.")
        goals.create("overgoal", "Money & Resources", description="Budgeting and finances.")
        goals.create("overgoal", "Relationships & Belonging",
                     description="Family, friends, and difficult conversations.")
        goals.close()
        scout = ScriptedScout(*[
            {"decision": "none", "reason": "", "question": "", "proposals": []}
            for _ in range(3)])
        companion = Companion(
            cfg=cfg, chat=ScriptedChat("Good.", "Good.", "I hear you."),
            proposal_scout=scout)

        companion.reply("I started exercising every morning.")
        companion.reply("I decided to make a weekly budget.")
        companion.reply("I keep avoiding a conversation with my sister because I feel anxious.")

        assert len(scout.contexts) == 3
        assert all(len(context["growth_tree"]) >= 4 for context in scout.contexts)
        assert scout.contexts[0]["signals"]["action"] is True
        assert scout.contexts[1]["signals"]["action"] is True
        assert scout.contexts[2]["signals"]["struggle"] is True
        companion.close()


def test_upwork_action_scout_can_propose_growth_and_existing_investigation_context():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore
        from livingpc.curiosity import CuriosityStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"),
                     filing_offer_min_chars=20)
        goals = GoalStore(cfg.memory_db_path)
        money = goals.create("overgoal", "Money & Resources")
        career = goals.create("subgoal", "Career & Building Things", parent_id=money)
        project = goals.create(
            "subgoal", "Run Upwork automation micro-test", parent_id=career,
            description="Validate freelance automation demand through a bounded experiment.")
        goals.close()
        investigations = CuriosityStore(cfg.memory_db_path)
        investigation_id = investigations.add_curiosity(
            "Find project filters that preserve interest in freelance work.",
            "Upwork Project Selection Filter")
        investigations.close()
        scout = ScriptedScout({
            "decision": "propose", "reason": "Two grounded updates.", "question": "",
            "proposals": [
                {"action": "create_leaf", "label": "Reposition Upwork profile",
                 "directive": "Rewrite the profile around Python automation with AI/LLM work.",
                 "reasoning": "This is the next concrete preparation step.",
                 "confidence": 0.94, "target_node_id": project, "priority": "normal"},
                {"action": "add_investigation_context", "label": "AI/LLM preference",
                 "directive": "I prefer automation projects that involve AI or LLMs.",
                 "reasoning": "This directly narrows the project-selection filter.",
                 "confidence": 0.96, "investigation_id": investigation_id},
            ],
        })
        companion = Companion(
            cfg=cfg, chat=ScriptedChat("That preference gives us a clear positioning."),
            proposal_scout=scout)

        rendered = companion.reply(
            "I definitely prefer automation that has AI/LLM involved; help me update my profile.")
        proposals = companion.pending_proposals()

        assert [item["action"] for item in proposals] == [
            "create_leaf", "add_investigation_context"]
        assert proposals[0]["target_node_id"] == project
        assert proposals[1]["investigation_id"] == investigation_id
        assert any(item["title"] == "Run Upwork automation micro-test"
                   and "freelance automation" in item["description"]
                   for item in scout.contexts[0]["growth_tree"])
        assert any(item["label"] == "Upwork Project Selection Filter"
                   for item in scout.contexts[0]["investigations"])
        assert "/file" not in rendered
        companion.close()


def test_proposal_free_chat_skips_scout_and_explicit_request_offers_enable():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        scout = ScriptedScout()
        companion = Companion(cfg=cfg, chat=ScriptedChat("unused"), proposal_scout=scout)
        companion.new_chat(proposals_enabled=False)

        rendered = companion.reply("Propose this as a Leaf in my Growth tree.")

        assert "proposal-free" in rendered
        assert "Turn on" in rendered
        assert scout.contexts == []
        assert companion.pending_proposals() == []
        companion.close()


def test_enabled_chat_can_decline_an_ungrounded_explicit_proposal_request():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        scout = ScriptedScout({
            "decision": "decline",
            "reason": "There is not yet a concrete action or unresolved pattern to place.",
            "question": "", "proposals": [],
        })
        companion = Companion(
            cfg=cfg, chat=ScriptedChat("We can keep talking about it."),
            proposal_scout=scout)

        rendered = companion.reply("Propose this in my Growth tree.")

        assert "not yet a concrete action" in rendered
        assert companion.pending_proposals() == []
        companion.close()


def test_proposal_mode_and_pending_cards_persist_per_chat():
    response = (
        '<<<faerie_proposal\n{"action":"start_investigation","label":"Study friction",'
        '"directive":"Understand what makes study sessions hard to begin."}\n'
        'faerie_proposal>>>'
    )
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        aware_chat = companion.chat_id
        companion.reply("I keep avoiding study even when I want to learn.")
        assert companion.pending_proposals()[0]["label"] == "Study friction"

        free_chat = companion.new_chat(proposals_enabled=False)
        assert companion.proposals_enabled is False
        assert companion.switch_chat(aware_chat) is True
        assert companion.proposals_enabled is True
        assert companion.pending_proposals()[0]["label"] == "Study friction"
        companion.close()

        reopened = Companion(cfg=cfg, chat=StubChat(), chat_id=free_chat)
        assert reopened.proposals_enabled is False
        assert reopened.pending_proposals() == []
        assert reopened.switch_chat(aware_chat) is True
        assert reopened.pending_proposals()[0]["label"] == "Study friction"
        reopened.close()


def test_existing_chat_database_migrates_to_proposals_enabled():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.companion.history import ChatStore

        path = os.path.join(directory, "m.db")
        database = sqlite3.connect(path)
        try:
            database.execute(
                "CREATE TABLE companion_chat (id TEXT PRIMARY KEY,title TEXT NOT NULL,"
                "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
            database.commit()
        finally:
            database.close()

        store = ChatStore(path)
        chat_id = store.create()

        assert store.proposals_enabled(chat_id) is True
        assert store.list()[0]["proposals_enabled"] is True


def test_approved_goal_progress_adds_evidence_without_completing_leaf():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Learning & Growth")
        korean = goals.create("subgoal", "Korean Language", parent_id=root)
        leaf = goals.create("task", "Study Korean grammar", parent_id=korean)
        goals.close()
        scout = ScriptedScout({
            "decision": "propose", "reason": "Equivalent Leaf exists.", "question": "",
            "proposals": [{
                "action": "record_goal_progress", "label": "Korean grammar progress",
                "directive": "Started studying Korean grammar again.",
                "reasoning": "The existing Leaf already owns this work.",
                "confidence": 0.95, "target_node_id": leaf,
            }],
        })
        companion = Companion(
            cfg=cfg, chat=ScriptedChat("Good restart."), proposal_scout=scout)
        companion.reply("I started studying Korean grammar again.")
        applied = companion.approve_proposal(0)

        goals = GoalStore(cfg.memory_db_path)
        try:
            node = goals.get(leaf)
            tree = goals.tree()
            pending = [tree]
            rendered = None
            while pending:
                candidate = pending.pop()
                if candidate["id"] == leaf:
                    rendered = candidate
                    break
                pending.extend(candidate.get("children", []))
            assert node["status"] == "active"
            assert rendered["evidence"][0]["source_kind"] == "companion_chat"
            assert "Started studying" in rendered["evidence"][0]["label"]
        finally:
            goals.close()
        assert "completion status did not change" in applied
        companion.close()


def test_approved_branch_proposal_persists_semantic_role():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        goals.close()
        response = (
            '<<<faerie_proposal\n{"action":"create_branch","label":"Freelance launch",'
            '"directive":"Land a first small automation contract.",'
            '"reasoning":"This is a finite outcome inside the money domain.",'
            f'"confidence":0.92,"target_node_id":{root},"semantic_role":"project"}}\n'
            'faerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        companion.reply("Help me start a freelance launch project.")
        companion.approve_proposal(0)

        goals = GoalStore(cfg.memory_db_path)
        try:
            child = goals.conn.execute(
                "SELECT id FROM goal_node WHERE parent_id=?", (root,)).fetchone()
            assert goals.semantic_role(int(child["id"]))["role"] == "project"
        finally:
            goals.close()
        companion.close()


def test_companion_exposes_core_commands_for_the_slash_menu():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        values = {item["value"].strip() for item in c.available_commands()}
        assert {"/browser", "/file", "/undo", "/projects", "/skills", "/teach", "/recalibrate"} <= values
        c.close()


def test_browser_slash_command_explains_usage_without_creating_a_task():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(cfg=cfg, chat=StubChat())
        rendered = companion.reply("/browser")
        assert "/browser <real form URL>" in rendered
        from livingpc.browser_assistant import BrowserTaskStore
        assert BrowserTaskStore(cfg.memory_db_path).list_active() == []
        companion.close()


def test_browser_slash_command_creates_task_without_model_proposal():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(cfg=cfg, chat=StubChat())
        rendered = companion.reply(
            "/browser https://forms.example/profile | "
            "Professional title: Automation Engineer")
        assert "Browser task ready" in rendered
        from livingpc.browser_assistant import BrowserTaskStore
        tasks = BrowserTaskStore(cfg.memory_db_path).list_active()
        assert len(tasks) == 1
        assert tasks[0]["origin"] == "https://forms.example"
        assert tasks[0]["status"] == "awaiting_domain_approval"
        assert "Professional title" not in tasks[0]
        companion.close()


def test_browser_slash_command_uses_attached_document_text():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(cfg=cfg, chat=StubChat())
        rendered = companion.reply(
            "/browser https://forms.example/profile",
            attachments=[{"kind": "text", "name": "resume.txt",
                          "text": "Professional title: Automation Engineer"}],
        )
        assert "Browser task ready" in rendered
        from livingpc.browser_assistant import BrowserTaskStore
        store = BrowserTaskStore(cfg.memory_db_path)
        public_task = store.list_active()[0]
        private_task = store.get(public_task["id"], private=True)
        assert "resume.txt" in private_task["source_context"]
        assert "Automation Engineer" in private_task["source_context"]
        companion.close()


def test_browser_slash_command_rejects_upwork():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(cfg=cfg, chat=StubChat())
        rendered = companion.reply(
            "/browser https://www.upwork.com/freelancers/settings | Title: Engineer")
        assert "Upwork does not permit" in rendered
        from livingpc.browser_assistant import BrowserTaskStore
        assert BrowserTaskStore(cfg.memory_db_path).list_active() == []
        companion.close()


def test_companion_can_propose_and_create_a_guarded_browser_task():
    response = (
        "I can prepare that as a visible browser task.\n"
        '<<<faerie_proposal\n{"action":"browser_task","label":"Update profile",'
        '"url":"https://forms.example/profile","directive":"Fill the visible profile form",'
        '"source_context":"Professional title: Automation Engineer",'
        '"reasoning":"The user explicitly requested this form update.","confidence":1.0}\n'
        "faerie_proposal>>>"
    )
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = companion.reply("Fill https://forms.example/profile using my title.")
        assert "Prepare a visible browser form task" in rendered
        assert companion.pending_proposal()["action"] == "browser_task"
        applied = companion.approve_proposal(0)
        assert "Browser task ready" in applied
        from livingpc.browser_assistant import BrowserTaskStore
        assert BrowserTaskStore(cfg.memory_db_path).list_active()[0]["origin"] == "https://forms.example"
        companion.close()


def test_companion_rejects_upwork_browser_proposals_and_prompts_for_draft_only():
    response = (
        '<<<faerie_proposal\n{"action":"browser_task","label":"Upwork profile",'
        '"url":"https://www.upwork.com/freelancers/settings","directive":"Fill profile",'
        '"source_context":"Title: Engineer","confidence":1.0}\nfaerie_proposal>>>'
    )
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        assert companion.pending_proposal() is None
        assert "couldn't be staged" in companion.reply("Fill my Upwork profile.")
        prompt = companion.system_prompt()
        assert "Never emit browser_task for Upwork" in prompt
        assert "upwork-profile-draft" in prompt
        companion.close()


def test_companion_keeps_distinct_proposal_batch_and_discards_exact_duplicate():
    first = (
        "Two patterns look worth following.\n"
        '<<<faerie_proposal\n{"action":"start_investigation","label":"Agency as a Signal",'
        '"description":"Track the trapped response across contexts.","reason":"It repeats."}\n'
        "faerie_proposal>>>\n"
        '<<<faerie_proposal\n{"action":"start_investigation","label":"Upwork Project Selection",'
        '"description":"Find work filters that preserve interest.","reason":"It affects the experiment."}\n'
        "faerie_proposal>>>\n"
        '<<<faerie_proposal\n{"action":"start_investigation","label":"Agency as a Signal",'
        '"description":"Duplicate wording.","reason":"Duplicate."}\n'
        "faerie_proposal>>>"
    )
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=ScriptedChat(first))
        rendered = c.reply("What patterns do you see?")
        proposals = c.pending_proposals()
        assert [item["label"] for item in proposals] == [
            "Agency as a Signal", "Upwork Project Selection"]
        assert rendered.count("Track as a new investigation:") == 2
        assert "Duplicate wording" not in rendered
        c.close()


def test_companion_can_approve_one_proposal_without_losing_the_other():
    response = (
        '<<<faerie_proposal\n{"action":"start_investigation","label":"Agency as a Signal",'
        '"description":"Track agency reactions.","reason":"It repeats."}\nfaerie_proposal>>>\n'
        '<<<faerie_proposal\n{"action":"start_investigation","label":"Upwork Project Selection",'
        '"description":"Find a useful selection filter.","reason":"It affects the next move."}\nfaerie_proposal>>>'
    )
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=ScriptedChat(response))
        c.reply("Track both.")
        result = c.approve_proposal(1)
        assert "Upwork Project Selection" in result
        assert [item["label"] for item in c.pending_proposals()] == ["Agency as a Signal"]
        from livingpc.curiosity import CuriosityStore
        store = CuriosityStore(cfg.memory_db_path)
        try:
            assert [item["label"] for item in store.list_curiosities("active")] == [
                "Upwork Project Selection"]
        finally:
            store.close()
        c.close()


def test_corrective_context_retires_only_the_affected_pending_proposal():
    response = (
        '<<<faerie_proposal\n{"action":"start_investigation",'
        '"label":"Draft rate guidance for profile",'
        '"description":"Research a defensible starting hourly rate."}\n'
        'faerie_proposal>>>\n'
        '<<<faerie_proposal\n{"action":"start_investigation",'
        '"label":"Upwork Project Selection Filter",'
        '"description":"Find which automation projects are worth pursuing."}\n'
        'faerie_proposal>>>'
    )
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(
            cfg=cfg, chat=ScriptedChat(response, "That is a reasonable starting approach."))
        companion.reply("I am planning my Upwork profile and project selection.")

        companion.reply(
            "I don't need to worry much about rate right now; I'll start at $50/hr.")

        assert [item["label"] for item in companion.pending_proposals()] == [
            "Upwork Project Selection Filter"]
        companion.close()


def test_corrected_proposal_is_replaced_while_unrelated_card_remains():
    first = (
        '<<<faerie_proposal\n{"action":"start_investigation",'
        '"label":"Draft rate guidance for profile",'
        '"description":"Research a defensible starting hourly rate."}\n'
        'faerie_proposal>>>\n'
        '<<<faerie_proposal\n{"action":"start_investigation",'
        '"label":"Upwork Project Selection Filter",'
        '"description":"Find which automation projects are worth pursuing."}\n'
        'faerie_proposal>>>'
    )
    refined = (
        'That simplifies the rate decision.\n'
        '<<<faerie_proposal\n{"action":"start_investigation",'
        '"label":"Rate feedback after first clients",'
        '"description":"Revisit rate only after real profile and client feedback."}\n'
        'faerie_proposal>>>'
    )
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(cfg=cfg, chat=ScriptedChat(first, refined))
        companion.reply("I am planning my Upwork profile and project selection.")

        companion.reply(
            "I don't want the current rate proposal; instead revisit rate after clients.")

        assert [item["label"] for item in companion.pending_proposals()] == [
            "Upwork Project Selection Filter", "Rate feedback after first clients"]
        companion.close()


def test_companion_can_dismiss_one_pending_proposal_without_applying_it():
    response = (
        '<<<faerie_proposal\n{"action":"start_investigation",'
        '"label":"Agency as a Signal","description":"Track agency reactions."}\n'
        'faerie_proposal>>>\n'
        '<<<faerie_proposal\n{"action":"start_investigation",'
        '"label":"Upwork Project Selection","description":"Find a useful filter."}\n'
        'faerie_proposal>>>'
    )
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        companion.reply("Track both possibilities.")

        assert companion.dismiss_proposal(0) is True
        assert [item["label"] for item in companion.pending_proposals()] == [
            "Upwork Project Selection"]
        assert companion.dismiss_proposal(9) is False
        companion.close()


def test_companion_suggests_and_approves_context_for_an_existing_investigation():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        from livingpc.curiosity import CuriosityStore
        store = CuriosityStore(cfg.memory_db_path)
        investigation_id = store.add_curiosity(
            "Understand when obligation creates a trapped response.", "Agency")
        store.close()
        context = (
            "The user notices an internal clock that treats coworker requests as urgent "
            "even without an actual deadline. Deliberately slowing down feels relieving "
            "and uncomfortable at the same time."
        )
        response = (
            "This looks connected to Agency.\n"
            f'<<<faerie_proposal\n{{"action":"add_investigation_context",'
            f'"label":"Agency","investigation_id":{investigation_id},'
            f'"directive":"{context}","reasoning":"It is another obligation-linked pull.",'
            '"confidence":0.91}\nfaerie_proposal>>>'
        )
        c = Companion(cfg=cfg, chat=ScriptedChat(response))
        prompt = c.system_prompt()
        assert f"id={investigation_id} Agency" in prompt
        assert "propose add_investigation_context instead" in prompt
        assert "never attach context silently" in prompt
        rendered = c.reply("Slowing down feels relieving but uncomfortable.")
        assert "Add this context to the **Agency** Investigation" in rendered
        assert c.pending_proposals()[0]["investigation_id"] == investigation_id

        applied = c.approve_proposal(0)
        assert "will inform its next questions and synthesis" in applied
        store = CuriosityStore(cfg.memory_db_path)
        try:
            saved = store.contexts(investigation_id)
            assert len(saved) == 1
            assert "internal clock" in saved[0]["note"]
            assert saved[0]["source_kind"] == "chat"
        finally:
            store.close()
        c.close()


def test_investigation_context_proposal_recovers_common_model_field_variants():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        from livingpc.curiosity import CuriosityStore
        store = CuriosityStore(cfg.memory_db_path)
        investigation_id = store.add_curiosity(
            "Understand agency as a physiological response.",
            "Agency as a Physiological Signal")
        store.close()
        response = (
            '<<<faerie_proposal\n{"action":"add_investigation_context",'
            '"investigation":"Agency as a Physiological Signal",'
            '"context":"Slowing down feels relieving and uncomfortable.",'
            '"reason":"This is another agency-linked body response."}\n'
            'faerie_proposal>>>'
        )
        c = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = c.reply("Can we add this context to the Agency investigation?")

        assert "Add this context to the **Agency as a Physiological Signal** Investigation" in rendered
        proposal = c.pending_proposals()[0]
        assert proposal["investigation_id"] == investigation_id
        assert proposal["directive"] == "Slowing down feels relieving and uncomfortable."
        assert proposal["reasoning"] == "This is another agency-linked body response."
        assert proposal["confidence"] == c.PROPOSAL_CONFIDENCE_GATE
        store = CuriosityStore(cfg.memory_db_path)
        try:
            assert store.contexts(investigation_id) == []
        finally:
            store.close()
        c.close()


def test_invalid_investigation_context_block_never_leaves_an_empty_chat_bubble():
    response = (
        '<<<faerie_proposal\n{"action":"add_investigation_context",'
        '"label":"Missing Investigation","context":"Some context."}\n'
        'faerie_proposal>>>'
    )
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = c.reply("Add this to the missing Investigation.")
        assert "couldn't be staged" in rendered
        assert c.pending_proposals() == []
        c.close()


def test_companion_persists_and_switches_multiple_chats():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        first = c.chat_id
        c.reply("first conversation")
        second = c.new_chat()
        c.reply("second conversation")
        assert second != first
        assert len(c.list_chats()) == 2
        assert c.switch_chat(first) is True
        assert c.history[0]["content"] == "first conversation"
        c.close()

        reopened = Companion(cfg=cfg, chat=StubChat(), chat_id=second)
        assert reopened.history[0]["content"] == "second conversation"
        reopened.close()


def test_companion_deletes_conversations_and_falls_back_from_active_chat():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        first = c.chat_id
        c.reply("first conversation")
        second = c.new_chat()
        c.reply("second conversation")

        assert c.delete_chat(second) is True
        assert c.chat_id != second
        assert all(chat["id"] != second for chat in c.list_chats())
        assert c.switch_chat(second) is False

        assert c.delete_chat(first) is True
        assert c.chat_id
        assert c.list_chats()
        c.close()


def test_companion_prompt_includes_confirmed_inferences():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        from livingpc.inference import InferenceStore
        inf = InferenceStore(cfg.memory_db_path)
        cid = inf.add_candidate("focus", "You lock in late at night.")
        inf.confirm(cid)
        inf.close()

        c = Companion(cfg=cfg, chat=StubChat())
        sysp = c.system_prompt()
        assert "You lock in late at night." in sysp
        assert "PATTERNS YOU'VE CONFIRMED" in sysp
        c.close()


def test_companion_prompt_shows_nothing_confirmed_yet_when_empty():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        assert "(nothing confirmed yet)" in c.system_prompt()
        c.close()


def test_companion_prompt_includes_active_curiosity_and_open_question():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        from livingpc.curiosity import CuriosityStore
        store = CuriosityStore(cfg.memory_db_path)
        cur_id = store.add_curiosity("help me get fit", "fitness")
        store.add_item(cur_id, "question", "How many days a week can you realistically train?")
        store.close()

        c = Companion(cfg=cfg, chat=StubChat())
        sysp = c.system_prompt()
        assert "fitness" in sysp
        assert "help me get fit" in sysp
        assert "How many days a week can you realistically train?" in sysp
        assert "GOALS / CURIOSITIES" in sysp
        c.close()


def test_companion_prompt_excludes_archived_curiosities():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        from livingpc.curiosity import CuriosityStore
        store = CuriosityStore(cfg.memory_db_path)
        archived_id = store.add_curiosity("learn piano", "piano")
        store.set_status(archived_id, "archived")
        store.close()

        c = Companion(cfg=cfg, chat=StubChat())
        sysp = c.system_prompt()
        assert "learn piano" not in sysp
        assert "(no active goals/curiosities yet)" in sysp
        c.close()


def test_calibration_status_has_no_example_text():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        status = c.calibration_status()
        first = status["sections"][0]["attributes"][0]
        assert "example" not in first
        c.close()


def test_calibration_skips_count_as_covered_for_ui_completion():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        from livingpc import soul_calibration
        for field in soul_calibration.FIELDS:
            status = c.calibration_save(field["section"], field["attribute"], "", skip=True)
        assert status["done"] == 0
        assert status["covered"] == status["total"]
        assert status["complete"] is True
        assert all(attr["state"] == "skipped"
                   for sec in status["sections"] for attr in sec["attributes"])
        c.close()


def test_calibration_skips_persist_across_companion_restarts():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        from livingpc import soul_calibration
        field = soul_calibration.FIELDS[0]
        c.calibration_save(field["section"], field["attribute"], "", skip=True)
        c.close()

        reopened = Companion(cfg=cfg, chat=StubChat())
        status = reopened.calibration_status()
        first = status["sections"][0]["attributes"][0]
        assert first["state"] == "skipped"
        assert status["covered"] == 1
        reopened.close()


def test_calibration_field_key_is_stable_across_localized_sections():
    from livingpc import soul_calibration
    field = soul_calibration.resolve_field("Favorites & Open Space", "favorite media and creators")
    korean = soul_calibration.resolve_field("좋아하는 것과 열린 공간", "favorite media and creators")
    assert field is not None and korean is not None
    assert soul_calibration.field_key(field) == soul_calibration.field_key(korean)


def test_calibration_edit_prefers_stable_storage_over_a_legacy_localized_row():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        mem = MemoryStore(cfg.memory_db_path)
        mem.upsert_core_profile_fact(
            "핵심 정체성", "other essential context", "Old localized answer",
            priority=72, source_kind="soul_calibration")
        mem.close()
        c = Companion(cfg=cfg, chat=StubChat())
        from livingpc import soul_calibration
        field = soul_calibration.resolve_field("Core Identity", "other essential context")

        status = c.calibration_save(
            field["section"], field["attribute"], "New edited answer")
        saved = next(
            attr for section in status["sections"]
            for attr in section["attributes"]
            if attr["attribute"] == "other essential context"
        )

        assert saved["value"] == "New edited answer"
        c.close()
        mem = MemoryStore(cfg.memory_db_path)
        active = [fact for fact in mem.core_profile_facts(limit=200)
                  if fact["attribute"] == "other essential context"]
        mem.close()
        assert [(fact["section"], fact["value"]) for fact in active] == [
            ("Core Identity", "New edited answer")
        ]


def test_calibration_uses_latest_pre_migration_localized_answer():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        mem = MemoryStore(cfg.memory_db_path)
        mem.upsert_core_profile_fact(
            "Core Identity", "other essential context", "Older English answer",
            priority=72, source_kind="soul_calibration")
        mem.upsert_core_profile_fact(
            "핵심 정체성", "other essential context", "Newer localized answer",
            priority=72, source_kind="soul_calibration")
        mem.close()

        c = Companion(cfg=cfg, chat=StubChat())
        status = c.calibration_status()
        saved = next(
            attr for section in status["sections"]
            for attr in section["attributes"]
            if attr["attribute"] == "other essential context"
        )

        assert saved["value"] == "Newer localized answer"
        c.close()


def test_calibration_preserves_multiline_answers():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        from livingpc import soul_calibration
        field = soul_calibration.FIELDS[0]

        status = c.calibration_save(
            field["section"], field["attribute"], "First line\n\nSecond line")
        saved = status["sections"][0]["attributes"][0]

        assert saved["state"] == "done"
        assert saved["value"] == "First line\n\nSecond line"
        c.close()


def test_persona_switch():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, persona_key="companion", chat=StubChat())
        assert c.persona.key == "companion"
        c.set_persona("gremlin")
        assert c.persona.key == "gremlin"
        assert "roast" in c.system_prompt().lower() or "gremlin" in c.system_prompt().lower()
        c.close()


def test_companion_selects_relevant_memory_with_limit():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(
            db_path=os.path.join(d, "e.db"),
            memory_db_path=os.path.join(d, "m.db"),
            companion_memory_max_items=1,
            companion_memory_max_chars=500,
        )
        mem = MemoryStore(cfg.memory_db_path)
        mem.add("Cooking", "favorite meal", "ramen")
        mem.add("League of Legends", "champion pool", "Caitlyn")
        mem.close()
        c = Companion(cfg=cfg, chat=StubChat())
        prompt = c.system_prompt("How should I play Caitlyn?")
        assert "Caitlyn" in prompt
        assert "ramen" not in prompt
        c.close()


def test_companion_uses_local_avatar_asset():
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    asset = ROOT / "livingpc/companion/assets/companion_avatar.jpg"
    assert asset.read_bytes().startswith(b"\xff\xd8\xff")   # JPEG signature
    assert '{{AVATAR_DATA_URL}}' in html
    assert 'id="avatarArt"' in html
    assert '<canvas id="cv"' not in html


def test_companion_uses_shared_leafy_background():
    # Same background photo + drifting motes as the Memory GUI, embedded as a
    # data URL (like the raccoon avatar) since this window loads html=... as
    # a raw string rather than a url=..., so relative asset paths won't resolve.
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    asset = ROOT / "livingpc/ui/assets/backgrounds/forest-ruins-main.jpg"
    assert asset.read_bytes().startswith(b"\xff\xd8\xff")   # JPEG signature
    assert '{{BACKGROUND_DATA_URL}}' in html
    assert 'id="motes"' in html
    assert 'mote-rise' in html


def test_companion_ui_has_no_blue_accent_left():
    # The panel used to be a plain dark-blue box; asked to switch fully to the
    # green/leafy palette that matches the background photo.
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    for removed in ("--cyan", "rgba(70,236,255", "rgba(110,140,240", "#46ecff"):
        assert removed not in html, removed
    assert "--green" in html


def test_companion_api_has_no_voice_methods():
    # The Python-side bridge dropped listen/set_listening/set_voice/poll/
    # hotkey_talk along with the UI change — plain send() is all that's left
    # for getting a reply out of the brain.
    import companion
    api = companion.Api()
    for removed in ("listen", "set_listening", "set_voice", "poll",
                    "hotkey_talk", "_ensure_ears", "_on_wake", "_hotkey_work"):
        assert not hasattr(api, removed), removed
    assert hasattr(api, "send")
    assert hasattr(api, "approve_proposal")
    assert hasattr(api, "get_reflection")
    assert hasattr(api, "refine_reflection")


def test_companion_ui_is_plain_text_chat_no_voice():
    # Companion is now a normal text chat window — no mic/listen/mute/wake
    # controls, no audio playback wiring. See companion.html + companion.py.
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    for removed in ("pywebview.api.listen", "pywebview.api.set_listening",
                    "pywebview.api.set_voice", "pywebview.api.poll",
                    "id=\"wake\"", "id=\"talk\"", "id=\"mute\"", "AudioContext"):
        assert removed not in html, removed
    assert 'id="textIn"' in html
    assert 'id="textSend"' in html
    assert 'pywebview.api.send' in html
    assert '<textarea id="textIn"' in html
    assert 'id="sidebar"' in html
    assert 'id="newChatMenu"' in html
    assert 'Growth + Investigation-aware' in html
    assert 'Proposal-free' in html
    assert 'id="proposalToggle"' in html
    assert 'set_chat_proposals_enabled' in html
    assert 'id="newChat"' in html
    assert 'e.key===\'Enter\'&&!e.shiftKey' in html
    assert 'user-select:text' in html


def test_companion_ui_has_no_persona_picker():
    # Companion/Coach/Gremlin buttons removed — always the default persona,
    # no switcher UI. The backend persona system itself (personas.py,
    # Companion.set_persona) is untouched; only this chat window's picker is gone.
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    for removed in ('id="personas"', "buildPersonas", "refreshPersonas",
                    "pywebview.api.set_persona", "pywebview.api.list_personas"):
        assert removed not in html, removed


def test_companion_api_toggle_maximize_without_window_is_a_safe_noop():
    import companion
    api = companion.Api()
    assert api._window is None
    assert api.toggle_maximize() is False


def test_companion_api_toggle_maximize_calls_window_toggle_fullscreen():
    import companion

    class _FakeWindow:
        def __init__(self):
            self.calls = 0

        def toggle_fullscreen(self):
            self.calls += 1

    api = companion.Api()
    api._window = _FakeWindow()
    assert api.toggle_maximize() is True
    assert api._window.calls == 1


def test_companion_api_minimize_calls_window_minimize():
    import companion

    class _FakeWindow:
        def __init__(self):
            self.calls = 0

        def minimize(self):
            self.calls += 1

    api = companion.Api()
    assert api.minimize() is False
    api._window = _FakeWindow()
    assert api.minimize() is True
    assert api._window.calls == 1


def test_companion_ui_has_maximize_button():
    # Frameless window, no native title bar — a custom button in the header
    # drives window.toggle_fullscreen() via the Api bridge. See companion.py.
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    assert 'id="maximize"' in html
    assert 'pywebview.api.toggle_maximize' in html
    assert 'id="minimize"' in html
    assert 'pywebview.api.minimize' in html


def test_companion_retries_history_restore_if_ready_event_was_missed():
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    assert "chatStateLoaded" in html
    assert "setTimeout(loadChatState" in html
    assert "pywebview.api.chat_state" in html


def test_replan_project_restructures_a_projects_leaves_in_one_approval():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore  # noqa: F811

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        project = goals.create("subgoal", "Run Upwork automation micro-test",
                               parent_id=root)
        # Completed work remains in the ordered history but does not consume
        # either of the two open horizon slots.
        choose = goals.create("task", "Choose automation niche", parent_id=project,
                              status="completed")
        listing = goals.create("task", "Write & post Upwork listing", parent_id=project)
        rate = goals.create("task", "Draft rate guidance for profile", parent_id=project)
        goals.close()
        response = (
            "Your rate decision reshapes this plan.\n"
            '<<<faerie_proposal\n{"action":"replan_project","label":"New micro-test plan",'
            '"directive":"Start from the profile update already underway.",'
            '"reasoning":"The $50/hr decision and profile work make the old order stale.",'
            f'"confidence":0.9,"target_node_id":{project},"steps":['
            '{"op":"create","title":"Update Upwork profile",'
            '"description":"Refresh profile; note $50/hr starting rate."},'
            f'{{"op":"rename","leaf_id":{listing},"new_title":"Post listing at $50/hr"}},'
            f'{{"op":"update","leaf_id":{choose},'
            '"description":"Only projects passing the AI/novelty filter."},'
            f'{{"op":"archive","leaf_id":{rate}}}]}}\n'
            'faerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = companion.reply(
            "Can you update the micro-test leaves based on this context?")
        assert "restructure the plan under" in rendered
        assert "1. Update Upwork profile *(new)*" in rendered
        assert "2. Post listing at $50/hr *(was: Write & post Upwork listing)*" in rendered
        assert "~~Draft rate guidance for profile~~" in rendered

        result = companion.approve_proposal(0)
        assert "Replanned" in result and "3 ordered steps" in result

        goals = GoalStore(cfg.memory_db_path)
        try:
            rows = goals.conn.execute(
                "SELECT id,status FROM goal_node WHERE parent_id=? "
                "ORDER BY position,id", (project,)).fetchall()
            active = [int(r["id"]) for r in rows if r["status"] != "archived"]
            titles = [goals.get(i)["title"] for i in active]
            assert titles == ["Update Upwork profile", "Post listing at $50/hr",
                              "Choose automation niche"]
            assert goals.get(choose)["description"] == (
                "Only projects passing the AI/novelty filter.")
            assert goals.get(rate)["status"] == "archived"
        finally:
            goals.close()
        companion.close()


def test_replan_project_requires_a_project_target_and_real_leaves():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        leaf = goals.create("task", "Choose automation niche", parent_id=root)
        goals.close()
        bad_target = (
            '<<<faerie_proposal\n{"action":"replan_project","label":"Plan",'
            f'"confidence":0.9,"target_node_id":{leaf},"steps":['
            '{"op":"create","title":"Something"}]}\nfaerie_proposal>>>'
        )
        bad_leaf = (
            '<<<faerie_proposal\n{"action":"replan_project","label":"Plan",'
            f'"confidence":0.9,"target_node_id":{root},"steps":['
            '{"op":"rename","leaf_id":99999,"new_title":"Ghost"}]}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(bad_target, bad_leaf))
        companion.reply("Replan it.")
        assert companion.pending_proposals() == []
        companion.reply("Try again.")
        assert companion.pending_proposals() == []
        companion.close()


def test_create_leaf_targeting_a_leaf_is_rejected_before_apply():
    # Regression: this used to validate, then blow up with ValueError at
    # approve time because a Leaf can't parent another Leaf.
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        branch = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        leaf = goals.create("task", "Write & post Upwork listing", parent_id=branch)
        goals.close()
        response = (
            '<<<faerie_proposal\n{"action":"create_leaf",'
            '"label":"Draft rate guidance for profile",'
            '"directive":"Research comparable listings.",'
            f'"confidence":0.82,"target_node_id":{leaf}}}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = companion.reply("File the rate guidance as a leaf.")
        assert companion.pending_proposals() == []
        assert "couldn't be staged" in rendered
        companion.close()


def test_truncated_proposal_block_never_renders_raw_json():
    # A reply cut off at the completion cap leaves an unterminated
    # <<<faerie_proposal tail; the chat must show a retry note, not raw JSON.
    response = (
        "Here's the restructure I'd propose.\n"
        '<<<faerie_proposal\n{"action": "replan_project", "label": "Replan", '
        '"confidence": 0.9, "target_node_id": 8, "steps": [{"op": "keep", "'
    )
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = companion.reply("Update the leaves based on this context.")
        assert "<<<faerie_proposal" not in rendered
        assert '"target_node_id"' not in rendered
        assert "cut off" in rendered
        assert "Here's the restructure I'd propose." in rendered
        assert companion.pending_proposals() == []
        companion.close()


def test_companion_chat_default_completion_window_fits_a_replan_block():
    from livingpc.companion.brain import ClaudeChat
    assert ClaudeChat.DEFAULT_MAX_TOKENS >= 1000


def test_gui_goal_catalog_bridge_lists_nodes_and_investigations():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore
        from livingpc.curiosity import CuriosityStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        goals.create("task", "Write & post Upwork listing", parent_id=root)
        goals.close()
        store = CuriosityStore(cfg.memory_db_path)
        store.add_curiosity("What filter keeps work interesting?",
                            "Upwork Project Selection Filter")
        store.close()

        import gui
        result = gui.GuiApi(cfg=cfg).goal_catalog()
        assert result["ok"] is True
        titles = {node["title"] for node in result["nodes"]}
        assert {"Money & Resources", "Write & post Upwork listing"} <= titles
        assert all({"id", "type", "title"} <= set(node) for node in result["nodes"])
        labels = [item["label"] for item in result["investigations"]]
        assert labels == ["Upwork Project Selection Filter"]


def test_leaf_horizon_caps_create_leaf_proposals_at_open_leaf_limit():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        cfg.goal_ai_leaf_horizon = 2
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        project = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        goals.create("task", "Update Upwork profile", parent_id=project)
        goals.create("task", "Post listing at $50/hr", parent_id=project)
        goals.close()
        response = (
            '<<<faerie_proposal\n{"action":"create_leaf","label":"Find 3 projects",'
            '"directive":"Browse and shortlist.",'
            f'"confidence":0.9,"target_node_id":{project}}}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = companion.reply("Queue up the project search too.")
        # Two Leaves are already open (committed + provisional): a third must
        # not stack — the plan should bend via replan instead. And the drop
        # must be visible, not a silent disappearance.
        assert companion.pending_proposals() == []
        assert "couldn't be staged" in rendered
        assert "leaf horizon" in rendered
        companion.close()


def test_pending_leaf_cards_reserve_capacity_and_semantic_identity():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        project = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        goals.close()
        response = (
            f'<<<faerie_proposal\n{{"action":"create_leaf","label":"Draft Upwork profile",'
            f'"directive":"Write the profile draft.","confidence":0.9,'
            f'"target_node_id":{project}}}\nfaerie_proposal>>>\n'
            f'<<<faerie_proposal\n{{"action":"create_leaf","label":"Draft Upwork freelancer profile",'
            f'"directive":"Draft the same profile.","confidence":0.9,'
            f'"target_node_id":{project}}}\nfaerie_proposal>>>\n'
            f'<<<faerie_proposal\n{{"action":"create_leaf","label":"Publish profile and scan postings",'
            f'"directive":"Publish, then scan suitable posts.","confidence":0.9,'
            f'"target_node_id":{project}}}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = companion.reply("I am updating and publishing my profile.")

        assert [item["label"] for item in companion.pending_proposals()] == [
            "Draft Upwork profile", "Publish profile and scan postings"]
        assert "Draft Upwork freelancer profile" not in rendered
        assert "couldn't be staged" in rendered
        companion.close()


def test_pending_leaf_in_another_chat_reserves_the_shared_horizon():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.companion.history import ChatStore
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        project = goals.create("overgoal", "Profile launch")
        goals.create("task", "Draft profile", parent_id=project)
        goals.close()
        response = (
            f'<<<faerie_proposal\n{{"action":"create_leaf",'
            f'"label":"Scan first postings","confidence":0.9,'
            f'"target_node_id":{project}}}}}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        chats = ChatStore(cfg.memory_db_path)
        other_chat = chats.create("Other planning chat")
        chats.replace_pending_proposals(other_chat, [{
            "action": "create_leaf", "label": "Publish profile",
            "directive": "Publish after review.", "confidence": 0.9,
            "target_node_id": project,
        }])

        rendered = companion.reply("Add the posting scan too.")

        assert companion.pending_proposals() == []
        assert "couldn't be staged" in rendered
        assert len(chats.pending_proposals(other_chat)) == 1
        companion.close()


def test_replan_approval_retires_pending_growth_cards_across_surfaces():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.companion.history import ChatStore
        from livingpc.goal_ai import AgentProposal, GoalAgentStore
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        project = goals.create("overgoal", "Profile launch")
        current = goals.create("task", "Draft profile", parent_id=project)
        goals.close()
        response = (
            f'<<<faerie_proposal\n{{"action":"replan_project",'
            f'"label":"Refresh profile plan","confidence":0.95,'
            f'"target_node_id":{project},"steps":['
            f'{{"op":"keep","leaf_id":{current}}},'
            '{"op":"create","title":"Publish profile and scan postings"}]}'
            '\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        agents = GoalAgentStore(cfg.memory_db_path)
        goal_ai_id = agents.add_proposal(project, AgentProposal(
            "create_child", project,
            {"type": "task", "title": "Old provisional step"},
            "Superseded by the complete replan"))
        chats = ChatStore(cfg.memory_db_path)
        other_chat = chats.create("Other planning chat")
        chats.replace_pending_proposals(other_chat, [{
            "action": "create_leaf", "label": "Another old provisional",
            "confidence": 0.9, "target_node_id": project,
        }])
        companion.reply("Replace the old tentative next steps.")

        result = companion.approve_proposal(0)

        assert "Replanned" in result
        assert agents.get_proposal(goal_ai_id)["status"] == "stale"
        assert chats.pending_proposals(other_chat) == []
        goals = GoalStore(cfg.memory_db_path)
        try:
            project_node = next(node for node in goals.tree()["children"]
                                if node["id"] == project)
            assert [leaf["title"] for leaf in project_node["children"]
                    if leaf["type"] == "task"
                    and leaf["status"] in {"active", "paused"}] == [
                        "Draft profile", "Publish profile and scan postings"]
        finally:
            goals.close()
            agents.close()
        companion.close()


def test_replan_owns_project_growth_but_keeps_unrelated_investigation_card():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        project = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        history = goals.create("task", "Brainstorm automation wins",
                               parent_id=project, status="completed")
        stale = goals.create("task", "Choose one automation task", parent_id=project)
        goals.close()
        response = (
            'The strategy changed, so I am replacing the stale queue.\n'
            f'<<<faerie_proposal\n{{"action":"replan_project","label":"Adaptive Upwork plan",'
            f'"directive":"Draft the profile, then provisionally publish and scan.",'
            f'"reasoning":"The user is applying to existing postings now.","confidence":0.94,'
            f'"target_node_id":{project},"steps":['
            f'{{"op":"keep","leaf_id":{history}}},'
            f'{{"op":"archive","leaf_id":{stale}}},'
            f'{{"op":"create","title":"Draft Upwork profile"}},'
            f'{{"op":"create","title":"Publish profile and first posting scan"}}]}}'
            '\nfaerie_proposal>>>\n'
            '<<<faerie_proposal\n{"action":"start_investigation",'
            '"label":"Upwork Project Selection Filter",'
            '"directive":"Learn which postings pass the AI and novelty filter."}'
            '\nfaerie_proposal>>>'
        )
        scout = ScriptedScout({
            "decision": "propose", "proposals": [{
                "action": "create_leaf", "label": "Draft Upwork profile",
                "directive": "Draft it.", "reasoning": "Current step.",
                "confidence": 0.91, "target_node_id": project,
            }, {
                "action": "create_leaf", "label": "Post Upwork profile and begin bidding",
                "directive": "Publish next.", "reasoning": "Next step.",
                "confidence": 0.91, "target_node_id": project,
            }],
        })
        companion = Companion(
            cfg=cfg, chat=ScriptedChat(response), proposal_scout=scout)
        rendered = companion.reply(
            "I am updating my Upwork profile and applying to existing postings.")

        proposals = companion.pending_proposals()
        assert [item["action"] for item in proposals] == [
            "replan_project", "start_investigation"]
        assert "Post Upwork profile and begin bidding" not in rendered
        assert rendered.count("restructure the plan under") == 1
        assert rendered.count("Track as a new investigation:") == 1
        companion.close()


def test_replan_owns_moves_into_project_and_project_level_renames():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        source = goals.create("subgoal", "Source project", parent_id=root)
        project = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        moving = goals.create("task", "Move this work", parent_id=source)
        now_leaf = goals.create("task", "Draft profile", parent_id=project)
        goals.close()
        response = (
            f'<<<faerie_proposal\n{{"action":"move_node","label":"Move work into Upwork",'
            f'"confidence":0.9,"target_node_id":{moving},"new_parent_id":{project}}}'
            '\nfaerie_proposal>>>\n'
            f'<<<faerie_proposal\n{{"action":"rename_node","label":"Rename Upwork project",'
            f'"confidence":0.9,"target_node_id":{project},"new_title":"Old framing"}}'
            '\nfaerie_proposal>>>\n'
            f'<<<faerie_proposal\n{{"action":"replan_project","label":"Adaptive Upwork plan",'
            f'"confidence":0.94,"target_node_id":{project},'
            f'"project_update":{{"title":"Current Upwork strategy"}},"steps":['
            f'{{"op":"keep","leaf_id":{now_leaf}}},'
            '{"op":"create","title":"Publish and scan postings"}]}'
            '\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))

        rendered = companion.reply("Update this project around the strategy I am using now.")

        assert [item["action"] for item in companion.pending_proposals()] == [
            "replan_project"]
        assert "Move work into Upwork" not in rendered
        assert "Rename Upwork project" not in rendered
        companion.close()


def test_standalone_leaf_rename_and_move_obey_duplicate_and_horizon_guards():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        source = goals.create("subgoal", "Source", parent_id=root)
        full = goals.create("subgoal", "Full project", parent_id=root)
        first = goals.create("task", "First step", parent_id=source)
        goals.create("task", "Second step", parent_id=source)
        goals.create("task", "Full now", parent_id=full)
        goals.create("task", "Full provisional", parent_id=full)
        goals.close()
        duplicate_rename = (
            f'<<<faerie_proposal\n{{"action":"rename_node","label":"Rename first",'
            f'"confidence":0.9,"target_node_id":{first},"new_title":"Second step"}}'
            '\nfaerie_proposal>>>'
        )
        overfull_move = (
            f'<<<faerie_proposal\n{{"action":"move_node","label":"Move first",'
            f'"confidence":0.9,"target_node_id":{first},"new_parent_id":{full}}}'
            '\nfaerie_proposal>>>'
        )
        companion = Companion(
            cfg=cfg, chat=ScriptedChat(duplicate_rename, overfull_move))

        first_reply = companion.reply("Rename the first step to match the second.")
        assert companion.pending_proposals() == []
        assert "couldn't be staged" in first_reply

        second_reply = companion.reply("Move that step into the full project.")
        assert companion.pending_proposals() == []
        assert "couldn't be staged" in second_reply
        companion.close()


def test_pending_rename_reserves_a_replacement_slot_not_an_extra_leaf():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Work")
        project = goals.create("subgoal", "Profile project", parent_id=root)
        current = goals.create("task", "Old profile step", parent_id=project)
        goals.close()
        response = (
            f'<<<faerie_proposal\n{{"action":"rename_node","label":"Refresh current step",'
            f'"confidence":0.9,"target_node_id":{current},'
            '"new_title":"Draft profile"}\nfaerie_proposal>>>\n'
            f'<<<faerie_proposal\n{{"action":"create_leaf",'
            '"label":"Publish profile and scan postings","confidence":0.9,'
            f'"target_node_id":{project}}}\nfaerie_proposal>>>\n'
            f'<<<faerie_proposal\n{{"action":"create_leaf",'
            '"label":"Message first client","confidence":0.9,'
            f'"target_node_id":{project}}}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))

        companion.reply("Refresh the current step and show me what comes next.")

        assert [item["action"] for item in companion.pending_proposals()] == [
            "rename_node", "create_leaf"]
        assert [item["label"] for item in companion.pending_proposals()] == [
            "Refresh current step", "Publish profile and scan postings"]
        companion.close()


def test_same_leaf_title_can_be_proposed_for_two_different_projects():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Work")
        first = goals.create("subgoal", "Client profile", parent_id=root)
        second = goals.create("subgoal", "Personal profile", parent_id=root)
        goals.close()
        response = (
            f'<<<faerie_proposal\n{{"action":"create_leaf","label":"Draft profile",'
            f'"directive":"Draft the client profile.","confidence":0.9,'
            f'"target_node_id":{first}}}\nfaerie_proposal>>>\n'
            f'<<<faerie_proposal\n{{"action":"create_leaf","label":"Draft profile",'
            f'"directive":"Draft the personal profile.","confidence":0.9,'
            f'"target_node_id":{second}}}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))

        companion.reply("I am starting both profile drafts.")

        assert [item["target_node_id"] for item in companion.pending_proposals()] == [
            first, second]
        companion.close()


def test_replan_version_snapshot_blocks_overwriting_a_newer_leaf_edit():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Work")
        project = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        leaf = goals.create("task", "Draft profile", parent_id=project,
                            description="Original description")
        goals.close()
        response = (
            f'<<<faerie_proposal\n{{"action":"replan_project","label":"Refresh plan",'
            f'"confidence":0.9,"target_node_id":{project},"steps":['
            f'{{"op":"update","leaf_id":{leaf},'
            '"description":"Description from the pending card"}]}'
            '\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        companion.reply("Refresh the plan description.")
        pending = companion.pending_proposal()
        assert set(pending["expected_versions"]) == {str(project), str(leaf)}

        goals = GoalStore(cfg.memory_db_path)
        goals.update(leaf, description="Newer user-approved description")
        goals.close()

        result = companion.approve_proposal(0)

        assert "became stale" in result and "nothing was applied" in result
        goals = GoalStore(cfg.memory_db_path)
        try:
            assert goals.get(leaf)["description"] == "Newer user-approved description"
        finally:
            goals.close()
        companion.close()


def test_unversioned_legacy_replan_card_is_retired_without_applying():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        project = goals.create("overgoal", "Project")
        leaf = goals.create("task", "Current", parent_id=project,
                            description="Keep this")
        goals.close()
        companion = Companion(cfg=cfg, chat=StubChat())
        companion._replace_pending_proposals([{
            "action": "replan_project", "label": "Legacy plan",
            "confidence": 0.9, "target_node_id": project,
            "steps": [{"op": "update", "leaf_id": leaf,
                       "description": "Unversioned overwrite"}],
        }])

        result = companion.approve_proposal(0)

        assert "became stale" in result and "nothing was applied" in result
        goals = GoalStore(cfg.memory_db_path)
        try:
            assert goals.get(leaf)["description"] == "Keep this"
        finally:
            goals.close()
        companion.close()


def test_replan_retires_legacy_growth_card_even_when_it_archives_its_target():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Work")
        project = goals.create("subgoal", "Old framing", parent_id=root)
        stale_leaf = goals.create("task", "Duplicate draft", parent_id=project)
        goals.close()
        response = (
            f'<<<faerie_proposal\n{{"action":"replan_project","label":"Repair project",'
            f'"confidence":0.9,"target_node_id":{project},'
            '"project_update":{"description":"Current strategy."},"steps":['
            f'{{"op":"archive","leaf_id":{stale_leaf}}}]}}'
            '\nfaerie_proposal>>>\n'
            '<<<faerie_proposal\n{"action":"start_investigation",'
            '"label":"Unrelated pattern","directive":"Track this separately."}'
            '\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        companion.reply("Repair the project and track the unrelated pattern.")
        replan, investigation = companion.pending_proposals()
        legacy_card = {
            "action": "rename_node", "label": "Old duplicate rename",
            "confidence": 0.9, "target_node_id": stale_leaf,
            "new_title": "Another duplicate",
        }
        companion._replace_pending_proposals([replan, legacy_card, investigation])

        assert "Replanned" in companion.approve_proposal(0)

        assert [item["action"] for item in companion.pending_proposals()] == [
            "start_investigation"]
        goals = GoalStore(cfg.memory_db_path)
        try:
            assert goals.get(stale_leaf)["status"] == "archived"
            assert goals.get(project)["description"] == "Current strategy."
        finally:
            goals.close()
        companion.close()


def test_typed_yes_keeps_multiple_cards_pending_until_one_is_chosen():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.curiosity import CuriosityStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        response = (
            '<<<faerie_proposal\n{"action":"start_investigation",'
            '"label":"First pattern","directive":"Follow the first pattern."}'
            '\nfaerie_proposal>>>\n'
            '<<<faerie_proposal\n{"action":"start_investigation",'
            '"label":"Second pattern","directive":"Follow the second pattern."}'
            '\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        companion.reply("Track both patterns.")

        result = companion.reply("yes")

        assert "separate proposal cards" in result
        assert "haven't applied anything" in result
        assert len(companion.pending_proposals()) == 2
        store = CuriosityStore(cfg.memory_db_path)
        try:
            assert store.list_curiosities("active") == []
        finally:
            store.close()
        assert "Started" in companion.approve_proposal(0)
        assert len(companion.pending_proposals()) == 1
        companion.close()


def test_create_leaf_is_revalidated_immediately_before_approval():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        project = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        goals.close()
        response = (
            f'<<<faerie_proposal\n{{"action":"create_leaf","label":"Draft Upwork profile",'
            f'"directive":"Write the draft.","confidence":0.9,'
            f'"target_node_id":{project}}}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        companion.reply("I am drafting the profile.")

        goals = GoalStore(cfg.memory_db_path)
        goals.create("task", "Draft Upwork profile", parent_id=project)
        goals.create("task", "Publish profile", parent_id=project)
        goals.close()

        result = companion.approve_proposal(0)
        assert "became stale" in result
        assert "nothing was applied" in result
        assert companion.pending_proposals() == []
        goals = GoalStore(cfg.memory_db_path)
        try:
            assert goals.open_leaf_count(project) == 2
        finally:
            goals.close()
        companion.close()


def test_replan_that_would_exceed_the_horizon_is_rejected():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        cfg.goal_ai_leaf_horizon = 2
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        project = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        keep_id = goals.create("task", "Update Upwork profile", parent_id=project)
        done_id = goals.create("task", "Brainstorm automation wins",
                               parent_id=project, status="completed")
        goals.close()
        too_many = (
            '<<<faerie_proposal\n{"action":"replan_project","label":"Overfull plan",'
            f'"confidence":0.9,"target_node_id":{project},"steps":['
            f'{{"op":"keep","leaf_id":{keep_id}}},'
            '{"op":"create","title":"Post listing"},'
            '{"op":"create","title":"Find 3 projects"}]}\nfaerie_proposal>>>'
        )
        within = (
            '<<<faerie_proposal\n{"action":"replan_project","label":"Bent plan",'
            f'"confidence":0.9,"target_node_id":{project},"steps":['
            f'{{"op":"keep","leaf_id":{done_id}}},'
            f'{{"op":"keep","leaf_id":{keep_id}}},'
            '{"op":"create","title":"Post listing"}]}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(too_many, within))
        companion.reply("Replan it with everything.")
        assert companion.pending_proposals() == []
        # Completed Leaves kept as record don't count against the horizon.
        companion.reply("Okay, bend the plan instead.")
        assert [p["label"] for p in companion.pending_proposals()] == ["Bent plan"]
        companion.close()


def test_prompt_carries_project_horizons_and_just_in_time_rules():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        project = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        goals.create("task", "Update Upwork profile", parent_id=project,
                     description="Anchor the $50/hr starting rate.")
        goals.create("task", "Post listing", parent_id=project)
        goals.set_project_signal(project, "currently_working")
        area = goals.create("subgoal", "Computer systems", parent_id=root)
        goals._set_semantic_role(area, "area", rationale="Owns ongoing computer systems.")
        goals.create("task", "Computer Whiteboard", parent_id=area)
        goals.close()
        companion = Companion(cfg=cfg, chat=StubChat())
        prompt = companion.system_prompt()
        assert "JUST-IN-TIME LEAVES" in prompt
        assert "ACTIVE PROJECT HORIZONS" in prompt
        assert "open[NOW]" in prompt and "open[TENTATIVE_NEXT]" in prompt
        assert "CURRENTLY_WORKING" in prompt
        whiteboard_line = next(line for line in prompt.splitlines()
                               if "Computer Whiteboard" in line)
        assert "open[" not in whiteboard_line
        assert "Anchor the $50/hr starting rate." in prompt
        assert "THE DEBRIEF MOMENT" in prompt
        companion.close()


def test_goalstore_leaf_horizon_reports_open_and_recent_done_leaves():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        project = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        now_leaf = goals.create("task", "Update Upwork profile", parent_id=project)
        next_leaf = goals.create("task", "Post listing", parent_id=project)
        done = goals.create("task", "Brainstorm wins", parent_id=project,
                            status="completed")
        old_done = goals.create("task", "Ancient history", parent_id=project,
                                status="completed")
        goals.conn.execute(
            "UPDATE goal_node SET completed_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (old_done,))
        goals.conn.commit()

        horizon = goals.leaf_horizon()
        assert goals.open_leaf_count(project) == 2
        entry = next(p for p in horizon if p["project_id"] == project)
        assert [leaf["id"] for leaf in entry["open"]] == [now_leaf, next_leaf]
        assert [leaf["id"] for leaf in entry["recent_done"]] == [done]
        assert "Upwork micro-test" in entry["path"]
        assert entry["attention_active"] is False
        assert entry["project_focus"] == {
            "highest_priority": False, "currently_working": False}
        goals.close()


def test_start_exploration_branches_a_thread_inside_the_investigation():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.curiosity import CuriosityStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        store = CuriosityStore(cfg.memory_db_path)
        cid = store.add_curiosity(
            "When did the trapped-feeling wire get laid and how does it shape decisions?",
            "Agency as a Physiological Signal")
        store.close()
        response = (
            "Same story, new route — this fits as an Exploration Thread.\n"
            '<<<faerie_proposal\n{"action":"start_exploration",'
            '"label":"Threat Monitoring vs. Task Completion",'
            '"directive":"After finishing work he still checks the computer; the '
            'checking delays anxiety rather than resolving it. What would satisfy '
            'the threat detector?",'
            f'"confidence":0.85,"investigation_id":{cid}}}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = companion.reply(
            "Should this be part of the same investigation, just a new route?")
        assert "Exploration Thread" in rendered
        # The thread title must survive normalization, not be overwritten by
        # the investigation's own label.
        assert "Threat Monitoring vs. Task Completion" in rendered

        result = companion.approve_proposal(0)
        assert "Branched" in result
        store = CuriosityStore(cfg.memory_db_path)
        try:
            threads = store.threads(cid)
            assert [t["title"] for t in threads] == [
                "Threat Monitoring vs. Task Completion"]
            assert "threat detector" in threads[0]["directive"]
        finally:
            store.close()
        companion.close()


def test_chat_can_rename_merge_and_archive_investigations():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.curiosity import CuriosityStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        store = CuriosityStore(cfg.memory_db_path)
        agency = store.add_curiosity("Trapped-feeling wiring.", "Agency as a Signal")
        fear = store.add_curiosity("Post-work checking.", "Fear-Based Pressure Cycle")
        spare = store.add_curiosity("Stale question.", "Old Thread")
        store.close()
        rename = (
            '<<<faerie_proposal\n{"action":"rename_investigation",'
            f'"label":"Agency as a Signal","investigation_id":{agency},'
            '"new_title":"Agency as a Physiological Signal","confidence":0.9}\n'
            'faerie_proposal>>>')
        merge = (
            '<<<faerie_proposal\n{"action":"merge_investigations",'
            f'"label":"Fear-Based Pressure Cycle","investigation_id":{fear},'
            f'"target_investigation_id":{agency},"confidence":0.9}}\n'
            'faerie_proposal>>>')
        archive = (
            '<<<faerie_proposal\n{"action":"archive_investigation",'
            f'"label":"Old Thread","investigation_id":{spare},"confidence":0.9}}\n'
            'faerie_proposal>>>')
        companion = Companion(cfg=cfg, chat=ScriptedChat(rename, merge, archive))
        companion.reply("Rename it to the full name.")
        assert "Agency as a Physiological Signal" in companion.approve_proposal(0)
        companion.reply("These are one story — merge them.")
        assert "Merged" in companion.approve_proposal(0)
        companion.reply("Archive the old one.")
        assert "Archived" in companion.approve_proposal(0)

        store = CuriosityStore(cfg.memory_db_path)
        try:
            by_id = {c["id"]: c for c in store.list_curiosities()}
            assert by_id[agency]["label"] == "Agency as a Physiological Signal"
            assert by_id[agency]["status"] == "active"
            assert by_id[fear]["status"] == "archived"
            assert by_id[spare]["status"] == "archived"
        finally:
            store.close()
        companion.close()


def test_approving_context_runs_an_immediate_fresh_round():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.curiosity import CuriosityStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        cfg.curiosity_backend = "stub"
        store = CuriosityStore(cfg.memory_db_path)
        cid = store.add_curiosity("Trapped-feeling wiring.",
                                  "Agency as a Physiological Signal")
        store.close()
        response = (
            '<<<faerie_proposal\n{"action":"add_investigation_context",'
            '"label":"Agency as a Physiological Signal",'
            '"directive":"The threat detector does not clock out with work.",'
            f'"confidence":0.9,"investigation_id":{cid}}}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        companion.reply("Add that to the investigation.")
        result = companion.approve_proposal(0)
        assert "Added — this is now approved context" in result
        assert "fresh round" in result and "queued" in result

        store = CuriosityStore(cfg.memory_db_path)
        try:
            assert [c["note"] for c in store.contexts(cid)] == [
                "The threat detector does not clock out with work."]
            # The stub model's round queued items immediately — the context
            # did not wait for the daily pass.
            assert len(store.open_items(cid)) >= 1
        finally:
            store.close()
        companion.close()


def test_prompt_knows_explorations_and_lists_open_threads():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.curiosity import CuriosityStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        store = CuriosityStore(cfg.memory_db_path)
        cid = store.add_curiosity("Trapped-feeling wiring.",
                                  "Agency as a Physiological Signal")
        store.add_thread(cid, "Threat Monitoring",
                         "What would satisfy the threat detector?")
        store.close()
        companion = Companion(cfg=cfg, chat=StubChat())
        prompt = companion.system_prompt()
        assert "EXPLORATION THREADS — A REAL FEATURE YOU KNOW ABOUT" in prompt
        assert "start_exploration" in prompt
        assert "merge_investigations" in prompt
        assert "exploration thread: Threat Monitoring" in prompt
        assert "same restructuring authority over Investigations" in prompt
        companion.close()


def test_replan_can_mark_finished_work_complete():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        project = goals.create("subgoal", "Upwork micro-test", parent_id=root)
        brainstorm = goals.create("task", "Brainstorm automation wins",
                                  parent_id=project)
        evaluate = goals.create("task", "Evaluate and choose one automation task",
                                parent_id=project)
        goals.close()
        response = (
            '<<<faerie_proposal\n{"action":"replan_project","label":"Debrief replan",'
            '"directive":"Brainstorm finished with four proven candidates.",'
            f'"confidence":0.9,"target_node_id":{project},"steps":['
            f'{{"op":"complete","leaf_id":{brainstorm}}},'
            f'{{"op":"keep","leaf_id":{evaluate}}}]}}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = companion.reply(
            "The brainstorm is done — four candidates with proof.")
        assert "✓ Brainstorm automation wins *(mark complete)*" in rendered

        result = companion.approve_proposal(0)
        assert "1 marked complete" in result

        goals = GoalStore(cfg.memory_db_path)
        try:
            assert goals.get(brainstorm)["status"] == "completed"
            assert goals.get(evaluate)["status"] == "active"
            # Completed work stops counting against the horizon.
            assert goals.open_leaf_count(project) == 1
        finally:
            goals.close()
        companion.close()


def test_completion_debrief_promotes_now_and_adds_one_provisional_leaf():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Money & Resources")
        project = goals.create("subgoal", "Upwork application experiment",
                               parent_id=root)
        now = goals.create("task", "Publish Upwork profile", parent_id=project)
        provisional = goals.create(
            "task", "Scan suitable postings", parent_id=project)
        goals.close()
        response = (
            'The profile is done, so the horizon should move with you.\n'
            f'<<<faerie_proposal\n{{"action":"replan_project",'
            f'"label":"Advance the Upwork horizon",'
            f'"directive":"Mark the profile complete and adapt the next two steps.",'
            f'"reasoning":"Completed work promotes the next step and makes only one new guess.",'
            f'"confidence":0.95,"target_node_id":{project},"steps":['
            f'{{"op":"complete","leaf_id":{now}}},'
            f'{{"op":"rename","leaf_id":{provisional},'
            f'"new_title":"Apply to first suitable posting",'
            f'"description":"Use the AI and novelty filter on the current scan."}},'
            f'{{"op":"create","title":"Review outcome and adapt the filter",'
            f'"description":"Treat this as provisional until the first application is sent."}}]}}'
            '\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        companion.reply("I finished publishing the profile.")

        assert [item["action"] for item in companion.pending_proposals()] == [
            "replan_project"]
        result = companion.approve_proposal(0)
        assert "1 marked complete" in result
        assert "1 added" in result

        goals = GoalStore(cfg.memory_db_path)
        try:
            assert goals.get(now)["status"] == "completed"
            horizon = next(item for item in goals.leaf_horizon()
                           if item["project_id"] == project)
            assert [item["title"] for item in horizon["open"]] == [
                "Apply to first suitable posting",
                "Review outcome and adapt the filter",
            ]
            assert goals.open_leaf_count(project) == 2
        finally:
            goals.close()
        companion.close()


def test_model_window_is_configurable_and_never_starts_on_assistant():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        cfg.companion_history_max_messages = 6
        chat = CapturingChat()
        companion = Companion(cfg=cfg, chat=chat)
        # Seed an odd-shaped history: a stray assistant message means a fixed
        # even slice would start on role=assistant (the BadRequestError case).
        companion.history = [{"role": "assistant", "content": "orphaned"}]
        for turn in range(4):
            companion.reply(f"turn {turn}")
        for messages in chat.calls:
            assert messages[0]["role"] == "user"
        # The window carries more than the old 12-message slice would imply:
        # with the cap at 6 the model still sees multiple prior exchanges.
        assert len(chat.calls[-1]) >= 5
        companion.close()


def test_out_of_credits_error_is_named_not_opaque():
    class Boom:
        def reply(self, *args, **kwargs):
            raise RuntimeError(
                "Your credit balance is too low to access the Anthropic API.")

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=Boom())
        out = c.reply("hello")
        assert "out of API credits" in out
        assert "BadRequestError" not in out
        c.close()


def test_prompt_is_honest_about_its_conversation_window():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        prompt = c.system_prompt()
        assert "WHAT YOU CAN SEE OF THIS CONVERSATION" in prompt
        assert "Never claim to have read back" in prompt
        c.close()


def test_prompt_instructs_proactive_replanning_over_clarifying_menus():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        prompt = c.system_prompt()
        assert "REPLANNING A PROJECT WHEN ITS PLAN GOES STALE" in prompt
        assert "replan_project" in prompt
        assert "without being asked" in prompt
        assert "never answer with a menu of clarifying options" in prompt
        assert "create_leaf never targets a Leaf" in prompt
        assert "in the SAME reply, never as a follow-up" in prompt
        assert "the card itself is the permission step" in prompt
        assert "PROSE IS NOT A PROPOSAL" in prompt
        assert "rides alongside" in prompt
        c.close()


def test_main_chat_can_set_move_and_clear_project_attention_signals():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Career & Building")
        upwork = goals.create("subgoal", "Run Upwork micro-test", parent_id=root)
        portfolio = goals.create("subgoal", "Refresh portfolio", parent_id=root)
        goals._set_semantic_role(
            upwork, "project", rationale="Explicit test Project.", source="user")
        goals._set_semantic_role(
            portfolio, "project", rationale="Explicit test Project.", source="user")
        goals.close()

        def proposal(target, kind, enabled=True):
            return (
                "I'll stage that Project marker for approval.\n"
                '<<<faerie_proposal\n'
                '{"action":"set_project_signal","label":"Project attention",'
                '"directive":"Use this Project attention marker.",'
                '"reasoning":"The user explicitly requested this Project marker.",'
                f'"confidence":1.0,"target_node_id":{target},'
                f'"signal_kind":"{kind}","enabled":'
                f'{str(enabled).lower()}}}\nfaerie_proposal>>>'
            )

        companion = Companion(cfg=cfg, chat=ScriptedChat(
            proposal(upwork, "highest_priority"),
            proposal(portfolio, "currently_working"),
            proposal(portfolio, "highest_priority"),
            proposal(portfolio, "currently_working", False),
        ))

        rendered = companion.reply("Make the Upwork Project my highest priority.")
        assert "mark **Run Upwork micro-test** as **Highest priority**" in rendered
        assert "Set — **Run Upwork micro-test** is now **Highest priority**" in (
            companion.approve_proposal(0))

        companion.reply("Mark the portfolio Project as currently working.")
        companion.approve_proposal(0)
        goals = GoalStore(cfg.memory_db_path)
        assert goals.project_signals() == {
            "highest_priority": upwork,
            "currently_working": portfolio,
        }
        goals.close()

        companion.reply("Actually, make the portfolio my highest priority too.")
        companion.approve_proposal(0)
        goals = GoalStore(cfg.memory_db_path)
        assert goals.project_signals() == {
            "highest_priority": portfolio,
            "currently_working": portfolio,
        }
        goals.close()

        rendered = companion.reply("Clear currently working from the portfolio.")
        assert "clear **Currently working** from **Refresh portfolio**" in rendered
        assert "Cleared — **Refresh portfolio** is no longer **Currently working**" in (
            companion.approve_proposal(0))
        goals = GoalStore(cfg.memory_db_path)
        assert goals.project_signals() == {
            "highest_priority": portfolio,
            "currently_working": None,
        }
        goals.close()
        companion.close()


def test_proposal_scout_can_recover_an_explicit_project_priority_request():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Career")
        project = goals.create("subgoal", "Ship portfolio", parent_id=root)
        goals._set_semantic_role(
            project, "project", rationale="Explicit test Project.", source="user")
        goals.close()
        scout = ScriptedScout({
            "decision": "propose", "reason": "Explicit Project priority request.",
            "question": "", "proposals": [{
                "action": "set_project_signal", "label": "Portfolio priority",
                "directive": "Make Ship portfolio the highest-priority Project.",
                "reasoning": "The user explicitly requested this marker.",
                "confidence": 1.0, "target_node_id": project,
                "signal_kind": "highest_priority", "enabled": True,
            }],
        })
        companion = Companion(
            cfg=cfg, chat=ScriptedChat("I'll update the priority."),
            proposal_scout=scout)

        rendered = companion.reply("Make Ship portfolio my highest priority.")

        assert len(scout.contexts) == 1
        assert scout.contexts[0]["signals"]["explicit"] is True
        assert companion.pending_proposal()["action"] == "set_project_signal"
        assert "mark **Ship portfolio** as **Highest priority**" in rendered
        companion.close()


def test_main_and_scout_priority_proposals_deduplicate_by_project_and_signal():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Career")
        project = goals.create("subgoal", "Ship portfolio", parent_id=root)
        goals._set_semantic_role(
            project, "project", rationale="Explicit test Project.", source="user")
        goals.close()
        main_reply = (
            "I'll stage the change.\n"
            '<<<faerie_proposal\n'
            '{"action":"set_project_signal","label":"Top portfolio priority",'
            '"directive":"Make the portfolio top priority.",'
            '"reasoning":"Explicit request.","confidence":1.0,'
            f'"target_node_id":{project},"signal_kind":"highest_priority",'
            '"enabled":true}\nfaerie_proposal>>>'
        )
        scout = ScriptedScout({
            "decision": "propose", "reason": "Explicit request.", "question": "",
            "proposals": [{
                "action": "set_project_signal", "label": "Highest priority Project",
                "directive": "Set the Project marker.",
                "reasoning": "Explicit request.", "confidence": 1.0,
                "target_node_id": project, "signal_kind": "highest_priority",
                "enabled": True,
            }],
        })
        companion = Companion(
            cfg=cfg, chat=ScriptedChat(main_reply), proposal_scout=scout)

        rendered = companion.reply("Make Ship portfolio my highest priority.")

        assert len(companion.pending_proposals()) == 1
        assert rendered.count("mark **Ship portfolio** as **Highest priority**") == 1
        companion.close()


def test_main_chat_rejects_project_attention_for_non_project_nodes():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Life")
        area = goals.create("subgoal", "Career", parent_id=root)
        goals._set_semantic_role(
            area, "area", rationale="Explicit test Area.", source="user")
        goals.close()
        response = (
            "I can't safely target a non-Project.\n"
            '<<<faerie_proposal\n'
            '{"action":"set_project_signal","label":"Career priority",'
            '"directive":"Mark Career as highest priority.",'
            '"reasoning":"Requested by the user.","confidence":1.0,'
            f'"target_node_id":{area},"signal_kind":"highest_priority",'
            '"enabled":true}\nfaerie_proposal>>>'
        )
        companion = Companion(cfg=cfg, chat=ScriptedChat(response))
        rendered = companion.reply("Make Career my highest priority.")
        assert "faerie_proposal" not in rendered
        assert companion.pending_proposals() == []
        goals = GoalStore(cfg.memory_db_path)
        assert goals.project_signals()["highest_priority"] is None
        goals.close()
        companion.close()


def test_main_chat_prompt_exposes_project_signal_action_and_current_marker():
    with tempfile.TemporaryDirectory() as directory:
        from livingpc.goals import GoalStore

        cfg = Config(db_path=os.path.join(directory, "e.db"),
                     memory_db_path=os.path.join(directory, "m.db"))
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Career")
        project = goals.create("subgoal", "Ship portfolio", parent_id=root)
        goals._set_semantic_role(
            project, "project", rationale="Explicit test Project.", source="user")
        goals.set_project_signal(project, "currently_working")
        goals.close()

        companion = Companion(cfg=cfg, chat=StubChat())
        prompt = companion.system_prompt()
        assert "PROJECT ATTENTION" in prompt
        assert "set_project_signal" in prompt
        assert '"signal_kind": "highest_priority"|"currently_working"' in prompt
        assert "[Branch; role=PROJECT; CURRENTLY_WORKING]" in prompt
        assert companion._proposal_signals(
            "Make Ship portfolio my highest priority.")["explicit"] is True
        companion.close()


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            fails += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns)-fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
