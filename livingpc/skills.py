"""Skills — user-extensible slash commands for the companion.

Drop a small .py file into `skills/` and the companion gains a command. Two
kinds:

  prompt skill  — no code runs; the file just declares a system prompt and the
                  user's arguments are sent through the normal chat backend.
  python skill  — the file defines run(args, ctx) -> str.

Skill file contract:

    SKILL = {"command": "roll", "description": "Roll dice: /roll 2d6",
             "kind": "python"}                       # or "prompt" + "system"
    def run(args, ctx):                              # python kind only
        return "🎲 7"

`ctx` gives a skill its capabilities: {"cfg": Config, "llm": fn(system, user)
-> str via the companion's chat backend, "memory_db": path}.

Self-extension (`/teach` in the companion) is approval-gated, matching the
house invariant that proposals remain pending until explicit approval: the
model DRAFTS a skill file, the user reads the full code in chat, and only an
explicit `/teach approve` writes it to disk. A skill file is ordinary Python
running with your user account's privileges — never approve code you haven't
read. Broken skill files never crash the companion; they show up in `/skills`
with their error instead.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from dataclasses import dataclass, field

from .config import _project_path
from .diagnostics import log_diag


def skills_dir_for(cfg) -> str:
    return _project_path(getattr(cfg, "skills_dir", "skills"))


@dataclass
class Skill:
    command: str = ""
    description: str = ""
    kind: str = "python"           # 'python' | 'prompt'
    system: str = ""               # prompt kind
    run: object = None             # python kind: callable(args, ctx) -> str
    path: str = ""
    error: str = ""                # non-empty => broken, listed but not callable


_COMMAND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_RESERVED = {"file", "undo", "projects", "skills", "teach"}


def _load_one(path: str) -> Skill:
    name = os.path.splitext(os.path.basename(path))[0]
    skill = Skill(path=path, command=name)
    try:
        spec = importlib.util.spec_from_file_location(f"ff_skill_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        meta = getattr(module, "SKILL", None)
        if not isinstance(meta, dict):
            raise ValueError("no SKILL dict")
        command = str(meta.get("command") or name).lower()
        if not _COMMAND_RE.match(command):
            raise ValueError(f"bad command name: {command!r}")
        if command in _RESERVED:
            raise ValueError(f"'/{command}' is a built-in command")
        skill.command = command
        skill.description = str(meta.get("description") or "")[:200]
        skill.kind = str(meta.get("kind") or "python").lower()
        if skill.kind == "prompt":
            skill.system = str(meta.get("system") or "")
            if not skill.system:
                raise ValueError("prompt skill without a 'system' prompt")
        elif skill.kind == "python":
            skill.run = getattr(module, "run", None)
            if not callable(skill.run):
                raise ValueError("python skill without a run(args, ctx) function")
        else:
            raise ValueError(f"unknown kind: {skill.kind}")
    except Exception as error:  # a broken file must never break loading
        skill.error = f"{type(error).__name__}: {error}"
    return skill


def load_skills(cfg) -> dict:
    """Load every skills/*.py. Returns {command: Skill} (broken ones included,
    keyed by filename, with .error set)."""
    directory = skills_dir_for(cfg)
    registry: dict = {}
    if not os.path.isdir(directory):
        return registry
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        skill = _load_one(os.path.join(directory, fname))
        registry[skill.command] = skill
    log_diag("skills", f"loaded={sum(1 for s in registry.values() if not s.error)} "
                       f"broken={sum(1 for s in registry.values() if s.error)}")
    return registry


def dispatch(skill: Skill, args: str, ctx: dict) -> str:
    """Run one skill. Never raises."""
    try:
        if skill.error:
            return f"(/{skill.command} is broken: {skill.error})"
        if skill.kind == "prompt":
            llm = ctx.get("llm")
            if not callable(llm):
                return "(no chat backend available for prompt skills)"
            return llm(skill.system, args or "(no input)")
        result = skill.run(args, ctx)
        return str(result) if result is not None else "(done)"
    except Exception as error:
        log_diag("skills", f"dispatch failed command={skill.command} "
                           f"error={type(error).__name__}")
        return f"(/{skill.command} failed: {type(error).__name__}: {error})"


# ------------------------------------------------------------------ /teach
TEACH_SYSTEM_PROMPT = """\
You write one small skill file for a personal assistant. The user describes a
tool; you produce a single Python file following this exact contract:

    SKILL = {"command": "<short-lowercase-name>",
             "description": "<one line, shown in /skills>",
             "kind": "python"}          # or "prompt" with a "system" key
    def run(args, ctx):                  # only for kind "python"
        ...
        return "reply text"

Rules:
- `args` is the raw text after the command; `ctx` is {"cfg", "llm", "memory_db"}.
  ctx["llm"](system, user) -> str calls the chat model when the skill needs one.
- Standard library only. No network calls unless the user explicitly asked.
- Small and readable — this code will be shown to the user for approval.
- Prefer kind "prompt" (no code execution) when the tool is just a reusation
  of the model with a fixed instruction.
- Return STRICT JSON only: {"filename": "<command>.py", "code": "<the file>"}
"""


def draft_skill(description: str, llm) -> dict:
    """Ask the model to draft a skill file. Returns {"filename", "code"} or
    {"error": ...}. `llm` is a callable(system, user) -> str."""
    try:
        raw = llm(TEACH_SYSTEM_PROMPT, description)
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
        filename = os.path.basename(str(data.get("filename") or ""))
        code = str(data.get("code") or "")
        if not filename.endswith(".py") or not code or "SKILL" not in code:
            return {"error": "the draft didn't follow the skill contract"}
        return {"filename": filename, "code": code}
    except Exception as error:
        return {"error": f"{type(error).__name__}: {error}"}


def install_skill(cfg, filename: str, code: str) -> str:
    """Write an approved skill file into skills_dir (backing up any previous
    version as .bak). Returns the path."""
    directory = skills_dir_for(cfg)
    os.makedirs(directory, exist_ok=True)
    safe = os.path.basename(filename)
    if not safe.endswith(".py"):
        raise ValueError("skill files must be .py")
    path = os.path.join(directory, safe)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            previous = f.read()
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(previous)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    log_diag("skills", f"installed {safe} chars={len(code)}")
    return path
