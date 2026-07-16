"""Configuration loading.

Defaults live here; an optional TOML file overrides them. Keeping config in
one typed place means the sampler, storage, and service all read the same knobs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(APP_DIR, "data")


def _data_path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def _project_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(APP_DIR, path)


@dataclass
class Config:
    # Product profile
    profile: str = "personal"        # 'personal' | 'launch'
    legacy_companion: bool = False   # standalone companion opt-out lock

    # Storage
    db_path: str = field(default_factory=lambda: _data_path("living_computer.db"))
    blob_dir: str = field(default_factory=lambda: _data_path("blobs"))
    memory_db_path: str = field(default_factory=lambda: _data_path("memory.db"))

    # Loop timing (seconds)
    tick: float = 2.0            # how often the loop wakes
    idle_limit: float = 60.0     # no-input seconds before we treat you as AFK
    max_interval: float = 45.0   # heartbeat: capture at least this often while active

    # Sampling sensitivity (perceptual-hash hamming distance, 0-64)
    default_threshold: int = 8
    app_thresholds: dict = field(default_factory=lambda: {
        "LeagueClient.exe": 4,
        "League of Legends.exe": 4,
    })

    # Privacy
    blocklist: list = field(default_factory=lambda: ["1Password.exe", "Bitwarden.exe"])

    # OCR
    ocr_enabled: bool = True

    # Extra collectors
    browser_history_enabled: bool = True
    browser_poll_seconds: float = 120.0   # how often to scan browser history
    clipboard_enabled: bool = True
    clipboard_poll_seconds: float = 4.0    # how often to check the clipboard

    # Maintenance: delete screenshots older than this many days during triage
    # (0 = delete right after each triage; the OCR text is kept regardless).
    blob_retention_days: int = 3

    # Triage LLM
    llm_backend: str = "claude"          # 'claude' (cloud) | 'stub' (offline)
    llm_model: str = "claude-sonnet-4-6" # used when llm_backend == 'claude'
    llm_timeout_seconds: float = 60.0      # fail visibly instead of waiting forever
    llm_max_retries: int = 0              # GUI can retry explicitly
    triage_memory_max_items: int = 30
    triage_memory_max_chars: int = 1600
    triage_memory_value_max_chars: int = 240
    # Facts (statements/supersessions) at/above this confidence auto-commit into
    # memory without review; less-confident facts + all questions stay pending.
    auto_commit_confidence: float = 0.75

    # Companion (Phase 2)
    companion_backend: str = "claude"        # 'claude' | 'stub'
    companion_model: str = "claude-sonnet-4-6"
    companion_proposal_scout_backend: str = ""  # '' => companion_backend
    companion_proposal_scout_model: str = "claude-haiku-4-5"
    companion_memory_max_items: int = 20
    # How many recent chat messages the companion model actually sees each
    # turn. Proposal cards + approvals eat slots fast — 12 was ~3 real
    # exchanges, which made the model confidently "forget" this conversation.
    # History past the window is stored but invisible to the model.
    companion_history_max_messages: int = 30
    companion_memory_max_chars: int = 6000
    companion_memory_value_max_chars: int = 500
    companion_inference_max_items: int = 10  # confirmed beliefs shown in the chat prompt
    companion_curiosity_max_items: int = 8   # active curiosities shown in the chat prompt
    companion_lifecycle_context_enabled: bool = True
    companion_lifecycle_context_max_chars: int = 16000
    companion_voice: bool = True             # speak replies aloud (TTS)
    companion_tts_engine: str = "pyttsx3"    # 'pyttsx3' | 'piper'
    companion_piper_model: str = ""          # path to a .onnx voice (if engine=piper)
    # Voice input (milestone D)
    whisper_model: str = "base"              # faster-whisper size: tiny/base/small/...
    whisper_device: str = "auto"             # 'auto' | 'cuda' | 'cpu'
    companion_wake_phrase: str = "hey faerie"
    companion_ptt_hotkey: str = "ctrl+alt+f" # global push-to-talk (needs 'keyboard')

    # Inference engine (Phase B+): forms bold psychological hypotheses from
    # behaviour + dwell on a frequent loop; a deeper pass runs nightly.
    inference_backend: str = "claude"           # 'claude' | 'stub'
    inference_model: str = "claude-haiku-4-5"   # cheap/fast for the ~20-30 min loop
    inference_nightly_model: str = "claude-sonnet-4-6"  # deeper nightly pass
    inference_lookback_hours: float = 1.0       # window used when no watermark yet
    inference_max_candidates: int = 6           # evidence items gathered per run (cap)
    inference_memory_max_items: int = 24
    inference_memory_max_chars: int = 2000
    # Evidence accumulation: a claim is only shown for yes/no once its hybrid
    # confidence reaches the gate; below it a theme shows only as "forming".
    inference_surface_confidence: float = 0.80  # the gate (>= this graduates)
    inference_min_evidence: int = 3             # min independent evidence to graduate
    inference_max_themes_per_run: int = 1       # one bounded Sonnet synthesis per daily run
    # Cadence (Phase E): the daemon fires the loop this often, plus one deeper
    # nightly pass. Set inference_scheduler_enabled=false to run only on demand.
    inference_scheduler_enabled: bool = True
    inference_schedule: str = "daily"           # 'daily' | 'manual'
    inference_interval_minutes: float = 1440.0  # legacy compatibility; daily mode ignores this
    inference_nightly_hour: int = 20            # local daily AI cycle hour (0-23)
    inference_poll_seconds: float = 30.0        # how often the scheduler checks
    companion_reflection_enabled: bool = True   # volunteer beliefs back in chat
    companion_reflection_min_turns: int = 4     # min exchanges between reflections
    # Nightly triage: the daemon distils the day's activity into confident facts
    # (auto-committed) as part of the nightly pass, so no Windows task is needed.
    triage_nightly_enabled: bool = True
    # Backups: memory.db is the one irreplaceable file. The nightly pass
    # snapshots it into a rotating set (SQLite online backup, safe while open).
    backup_enabled: bool = True
    backup_dir: str = ""                        # empty => <data>/backups
    backup_keep: int = 14                       # snapshots retained
    # Consolidation (the scale plan): nightly hygiene pass that merges
    # near-duplicate active facts (newest kept; older copies closed like a
    # supersession, never deleted) and prunes stale rejections/evidence.
    consolidate_enabled: bool = True
    consolidate_value_similarity: float = 0.85  # token Jaccard to call values equal
    consolidate_rejection_retention_days: int = 90   # prompts only read last 14
    consolidate_evidence_retention_days: int = 180   # 0 = never prune evidence
    # Journal backfill: chronological import of exported journals (data/notion)
    # into memory, facts dated by their entries. See livingpc/journal_import.py.
    journal_dir: str = "data/notion"
    journal_import_model: str = "claude-sonnet-4-6"  # deep read, run rarely
    journal_min_confidence: float = 0.7              # commit gate for proposed facts
    journal_batch_max_chars: int = 24000             # per-model-call text cap
    # Local relevance pre-filter (zero API cost): drop short/duplicate/pasted-
    # advice entries and trim huge ones before anything is sent to the model.
    journal_filter_enabled: bool = True
    journal_filter_min_chars: int = 80               # entries shorter are noise
    journal_filter_min_score: float = 1.0            # insight-density threshold
    journal_filter_similarity: float = 0.90          # near-duplicate cutoff
    journal_entry_max_chars: int = 6000              # trim head+tail beyond this
    # Clarifying questions: when a memory value hedges ("possibly a relative",
    # "family-adjacent"), ask instead of leaving the guess in place. Detection
    # is free (regex); only phrasing the question and folding the answer back
    # in touches a model. See livingpc/clarify.py.
    clarify_backend: str = ""                        # '' => falls back to inference_backend
    clarify_model: str = "claude-haiku-4-5"           # cheap; one call per new hedge
    clarify_scan_after_import: bool = True            # auto-scan after a real journal import
    clarify_scan_limit: int = 20                      # new clarifications queued per scan
    # Age below which a personally-experienced memory is flagged as
    # implausible (needs a birth date set via the Clarify tab; silent without one).
    clarify_min_plausible_age: float = 2.0

    # Curiosity: a directive the user sets ("help me hit my fitness goals")
    # that the engine actively pursues — generating questions and (once
    # grounded in confirmed beliefs) suggestions, on a schedule plus on
    # demand. See livingpc/curiosity.py.
    curiosity_backend: str = ""                       # '' => falls back to inference_backend
    curiosity_model: str = "claude-haiku-4-5"
    curiosity_interval_minutes: int = 1440             # one coordinated daily pass
    curiosity_scan_limit_greatest: int = 5             # items/round for the "greatest" curiosity
    curiosity_scan_limit_background: int = 2           # items/round for the rest
    curiosity_question_min_confidence: float = 0.70    # "not redundant" gate
    curiosity_suggestion_min_confidence: float = 0.80  # "grounded in confirmed beliefs" gate
    curiosity_max_open_per_curiosity: int = 6          # pause generation until the user catches up
    curiosity_metrics_enabled: bool = True
    curiosity_checkin_hour: int = 19                   # eligible time; shared weekly gate still applies
    curiosity_calibration_days: int = 7                # local-only before any dashboard migration
    curiosity_chart_days: int = 30

    # Hierarchical GoalAI: bounded per-node agents, proposal-only authority.
    goal_ai_enabled: bool = True
    goal_ai_backend: str = ""                   # '' => falls back to inference_backend
    goal_ai_schedule: str = "daily_dirty"       # only dirty active nodes run automatically
    goal_ai_interval_minutes: int = 1440         # legacy/staleness display compatibility
    goal_ai_batch_size: int = 12
    goal_ai_leaf_model: str = "claude-haiku-4-5"
    goal_ai_parent_model: str = "claude-sonnet-4-6"
    # Completion handoffs are infrequent, high-leverage synthesis calls. Empty
    # deliberately inherits the stronger parent route rather than Leaf chat.
    goal_ai_handoff_model: str = ""
    goal_ai_context_max_chars: int = 14000
    goal_ai_max_open_proposals: int = 3
    # Just-in-time planning: max open (non-completed) Leaves per project.
    # 2 = one committed step plus one provisional next; the next real step is
    # decided in chat at the completion debrief, not pre-generated.
    goal_ai_leaf_horizon: int = 2
    goal_ai_notifications: bool = True
    goal_relevance_stale_days: int = 30         # gentle check after a month without movement
    # Shared unsolicited-reflection rhythm. Explicit /remind reminders bypass
    # this gate because the user requested them directly.
    reflection_min_days: int = 7                # global maximum: one prompt per week
    reflection_quiet_start_hour: int = 21       # no reflective toasts overnight
    reflection_quiet_end_hour: int = 8
    reflection_backlog_limit: int = 3           # do not build an anxiety queue
    reflection_snooze_base_days: int = 3        # 3, 6, 12, 24 (capped at 28)
    reflection_ignore_suppress_days: int = 30   # repeated ignores extend suppression
    # Notion sync: by default mirrors each curiosity to a child page under a
    # parent Notion page. When notion_curiosity_database_id is configured,
    # curiosities become database rows and only their marked managed section
    # is rewritten, preserving user-authored blocks around it.
    notion_sync_enabled: bool = True
    notion_api_key: str = ""                # falls back to NOTION_API_KEY env var
    notion_parent_page_id: str = "393783c5-8191-80fa-803a-e86023c0ec49"  # "Faerie Fire" under center.exe
    notion_curiosity_database_id: str = ""  # opt-in; leave empty until the Life Hub is approved
    notion_curiosity_data_source_id: str = ""  # optional; auto-resolved for one-source databases
    notion_curiosity_cover_file_upload_ids: list[str] = field(default_factory=lambda: [
        "394783c5-8191-8113-8b3c-00b28ce60582",
        "394783c5-8191-81eb-a3a4-00b2a80ba498",
        "394783c5-8191-81f2-b335-00b2d7b81c5f",
    ])

    # Desktop toasts: import/dry-run finished, nightly "N inferences waiting"
    # reminder, and a heads-up when a hypothesis crosses the gate.
    notifications_enabled: bool = True
    notify_on_graduation: bool = True                # toast when a claim graduates

    # Phase 2 — real-time assistant
    assistant_model: str = "claude-sonnet-4-6"
    assistant_hotkey: str = "ctrl+shift+space"
    assistant_memory_max_items: int = 20
    assistant_memory_max_chars: int = 6000
    assistant_memory_value_max_chars: int = 500

    # Filing engine: brain dumps -> living project documents (Markdown, append
    # only, undoable). One inbox: the companion chat (/file) or the CLI
    # (tools/file_dump.py). See docs/filing_plan.md and livingpc/filing.py.
    projects_dir: str = "projects"              # gitignored personal project docs
    filing_backend: str = "claude"              # 'claude' | 'stub' | 'ollama'
    filing_model: str = "claude-sonnet-4-6"     # filing decisions are the hard part
    filing_min_confidence: float = 0.6          # below this: clarify, don't guess
    filing_auto_offer: bool = True              # companion offers to file long messages
    filing_offer_min_chars: int = 600
    filing_to_memory: bool = False              # also save dumps for journal import
    filing_journal_dir: str = "data/filed_dumps"
    filing_catalog_max_chars: int = 8000        # cap on the doc catalog sent to the model
    filing_ollama_url: str = "http://localhost:11434"   # used when backend == 'ollama'
    filing_ollama_model: str = "qwen2.5:14b"

    # Skills: user-extensible slash commands (skills/*.py) and on-demand
    # workflow skills (skills/<name>/SKILL.md). /teach drafts new ones in
    # chat; installs only on explicit approval. See livingpc/skills.py.
    skills_dir: str = "skills"
    workflow_max_active: int = 3            # loaded SKILL.md bodies per chat (oldest evicted)
    workflow_body_max_chars: int = 20000    # a body over this is broken, not truncated
    # Explicit, user-approved browser form assistance. This is separate from
    # passive browser-history capture and remains available in the launch
    # profile because it only runs after a Command Center approval.
    browser_assistant_enabled: bool = True
    browser_assistant_profile_dir: str = "data/browser-profile"
    # Reminders (/remind): fired as toasts by the daemon's 30s poll.
    reminders_enabled: bool = True

    def threshold_for(self, app: str) -> int:
        """Per-app sampling threshold, falling back to the default."""
        return self.app_thresholds.get(app, self.default_threshold)


def load(path: str | None = None) -> Config:
    """Load config from a TOML file if present, else return defaults."""
    cfg = Config()
    config_path = _project_path(path) if path else None
    if config_path and os.path.exists(config_path):
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError:  # pragma: no cover
            import tomli as tomllib  # type: ignore
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        known = {f.name for f in fields(Config)}
        for key, value in data.items():
            if key in known:
                setattr(cfg, key, value)
    cfg.db_path = _project_path(cfg.db_path)
    cfg.blob_dir = _project_path(cfg.blob_dir)
    cfg.memory_db_path = _project_path(cfg.memory_db_path)
    cfg.browser_assistant_profile_dir = _project_path(cfg.browser_assistant_profile_dir)
    if getattr(cfg, "profile", "personal") == "launch":
        cfg.ocr_enabled = False
        cfg.browser_history_enabled = False
        cfg.clipboard_enabled = False
        cfg.inference_scheduler_enabled = False
        cfg.triage_nightly_enabled = False
        cfg.notion_sync_enabled = False
        cfg.companion_lifecycle_context_enabled = False
    return cfg
