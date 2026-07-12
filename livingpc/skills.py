"""Skills — user-extensible slash commands and workflows for the companion.

Drop a small .py file into `skills/` and the companion gains a command. Three
kinds:

  prompt skill   — no code runs; the file just declares a system prompt and the
                   user's arguments are sent through the normal chat backend.
  python skill   — the file defines run(args, ctx) -> str.
  workflow skill — a folder `skills/<name>/SKILL.md` with `---` frontmatter
                   (name, description, optional `disable-model-invocation:
                   true`). Only the one-line description rides in every prompt;
                   the full body loads on demand — when the model requests it
                   mid-conversation or the user types /<name>. No code runs.

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
    kind: str = "python"           # 'python' | 'prompt' | 'workflow'
    system: str = ""               # prompt kind
    run: object = None             # python kind: callable(args, ctx) -> str
    path: str = ""
    error: str = ""                # non-empty => broken, listed but not callable
    body: str = ""                 # workflow kind: the SKILL.md body
    dir: str = ""                  # workflow kind: the skill's folder
    model_invocable: bool = True   # False => never in the menu; /name only


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


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal '---'-fenced `key: value` frontmatter (no YAML dependency).
    Returns (meta, body); ({}, text) when the fence is missing or unclosed."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:]).strip()
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value.lower() in ("true", "false"):
            value = value.lower() == "true"
        if key:
            meta[key] = value
    return {}, text


_WORKFLOW_BODY_MAX_DEFAULT = 20000  # ≈5k tokens; over this => broken, not truncated


def _load_workflow(dirpath: str, max_body_chars: int = _WORKFLOW_BODY_MAX_DEFAULT) -> Skill:
    folder = os.path.basename(os.path.normpath(dirpath))
    path = os.path.join(dirpath, "SKILL.md")
    skill = Skill(path=path, dir=dirpath, command=folder, kind="workflow")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        meta, body = _parse_frontmatter(text)
        if not meta:
            raise ValueError("SKILL.md must start with '---' frontmatter "
                             "(name, description)")
        name = str(meta.get("name") or "").lower()
        if not _COMMAND_RE.match(name):
            raise ValueError(f"bad skill name: {name!r}")
        if name != folder:
            raise ValueError(f"frontmatter name {name!r} must match the folder "
                             f"name {folder!r}")
        if name in _RESERVED:
            raise ValueError(f"'/{name}' is a built-in command")
        description = str(meta.get("description") or "").strip()
        if not description:
            raise ValueError("workflow skill without a 'description' — it is "
                             "how the model decides when to load it")
        if not body.strip():
            raise ValueError("SKILL.md has no body after the frontmatter")
        if len(body) > max_body_chars:
            raise ValueError(f"SKILL.md body too large ({len(body)} chars > "
                             f"{max_body_chars}) — move detail into references")
        skill.description = description[:300]
        skill.body = body
        skill.model_invocable = meta.get("disable-model-invocation") is not True
    except Exception as error:  # a broken folder must never break loading
        skill.error = f"{type(error).__name__}: {error}"
    return skill


def load_skills(cfg) -> dict:
    """Load every skills/*.py plus every skills/*/SKILL.md workflow folder.
    Returns {command: Skill} (broken ones included, with .error set)."""
    directory = skills_dir_for(cfg)
    registry: dict = {}
    if not os.path.isdir(directory):
        return registry
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        skill = _load_one(os.path.join(directory, fname))
        registry[skill.command] = skill
    max_body = getattr(cfg, "workflow_body_max_chars", _WORKFLOW_BODY_MAX_DEFAULT)
    for fname in sorted(os.listdir(directory)):
        path = os.path.join(directory, fname)
        if fname.startswith("_") or not os.path.isdir(path):
            continue
        if not os.path.isfile(os.path.join(path, "SKILL.md")):
            continue
        skill = _load_workflow(path, max_body_chars=max_body)
        if skill.command in registry:
            # Never shadow a loaded skill: list the folder as broken under a
            # key no typed /command can ever match.
            skill.error = skill.error or f"name collides with skills/{skill.command}.py"
            registry[skill.command + "@md"] = skill
        else:
            registry[skill.command] = skill
    log_diag("skills", f"loaded={sum(1 for s in registry.values() if not s.error)} "
                       f"workflow={sum(1 for s in registry.values() if s.kind == 'workflow' and not s.error)} "
                       f"broken={sum(1 for s in registry.values() if s.error)}")
    return registry


def workflow_menu(registry) -> str:
    """One '- name — description' line per model-invocable workflow skill.
    This (~100 tokens) is all that rides in every prompt; bodies load on
    demand. Empty string when there is nothing to advertise."""
    lines = [f"- {s.command} — {s.description}"
             for s in sorted(registry.values(), key=lambda s: s.command)
             if s.kind == "workflow" and not s.error and s.model_invocable]
    return "\n".join(lines)


def dispatch(skill: Skill, args: str, ctx: dict) -> str:
    """Run one skill. Never raises."""
    try:
        if skill.error:
            return f"(/{skill.command} is broken: {skill.error})"
        if skill.kind in ("prompt", "workflow"):
            # Workflow skills are normally activated brain-side (body injected
            # into the system prompt); this branch is the degraded fallback.
            llm = ctx.get("llm")
            if not callable(llm):
                return "(no chat backend available for prompt skills)"
            system = skill.body if skill.kind == "workflow" else skill.system
            return llm(system, args or "(no input)")
        result = skill.run(args, ctx)
        return str(result) if result is not None else "(done)"
    except Exception as error:
        log_diag("skills", f"dispatch failed command={skill.command} "
                           f"error={type(error).__name__}")
        return f"(/{skill.command} failed: {type(error).__name__}: {error})"


# ------------------------------------------------------------------ /teach
TEACH_SYSTEM_PROMPT = """\
You extend a personal assistant with one new skill. First classify the user's
request as exactly one of three types, then draft it:

  command   — a discrete action the user triggers as `/name args` (roll dice,
              convert units). Produce a Python skill file.
  workflow  — a multi-step process the assistant should follow whenever the
              conversation calls for it (a weekly review ritual, an essay
              planning method). Produce a SKILL.md.
  reference — knowledge the assistant should consult (a house writing style,
              personal terminology). Produce a SKILL.md; add
              `disable-model-invocation: true` to the frontmatter when it
              should only ever load on an explicit /name request.

Python skill file contract (type "command"):

    SKILL = {"command": "<short-lowercase-name>",
             "description": "<one line, shown in /skills>",
             "kind": "python"}          # or "prompt" with a "system" key
    def run(args, ctx):                  # only for kind "python"
        ...
        return "reply text"

- `args` is the raw text after the command; `ctx` is {"cfg", "llm", "memory_db"}.
  ctx["llm"](system, user) -> str calls the chat model when the skill needs one.
- Standard library only. No network calls unless the user explicitly asked.
- Small and readable — this code will be shown to the user for approval.
- Prefer kind "prompt" (no code execution) when the tool is just a reuse of
  the model with a fixed instruction.

SKILL.md contract (types "workflow" and "reference"):

    ---
    name: <lowercase-hyphens; becomes the folder name and the /command>
    description: <what it does + when to load it + how it differs from related
                  skills. This one line alone routes loading — never summarize
                  the steps themselves here.>
    ---
    <the full instructions: goal, stepwise procedure or reference content,
    output expectations, boundaries. One concern per skill. Keep it well
    under 4000 words.>

Return STRICT JSON only, exactly one of:
  {"type": "command", "filename": "<name>.py", "code": "<the file>"}
  {"type": "workflow", "name": "<name>", "skill_md": "<the SKILL.md content>"}
  {"type": "reference", "name": "<name>", "skill_md": "<the SKILL.md content>"}
"""


def draft_skill(description: str, llm, force_type: str | None = None) -> dict:
    """Ask the model to draft a skill. Returns {"type": "command", "filename",
    "code"} or {"type": "workflow"|"reference", "name", "skill_md"} or
    {"error": ...}. `llm` is a callable(system, user) -> str."""
    try:
        system = TEACH_SYSTEM_PROMPT
        if force_type in ("command", "workflow", "reference"):
            system += (f"\nThe user has fixed the type: {force_type} — do not "
                       "reclassify.")
        raw = llm(system, description)
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
        kind = str(data.get("type") or "").lower()
        if not kind:  # tolerate older/looser drafts: infer from shape
            kind = "command" if data.get("filename") else "workflow"
        if kind == "command":
            filename = os.path.basename(str(data.get("filename") or ""))
            code = str(data.get("code") or "")
            if not filename.endswith(".py") or not code or "SKILL" not in code:
                return {"error": "the draft didn't follow the skill contract"}
            return {"type": "command", "filename": filename, "code": code}
        if kind in ("workflow", "reference"):
            name = str(data.get("name") or "").lower()
            content = str(data.get("skill_md") or "")
            if not _COMMAND_RE.match(name) or name in _RESERVED:
                return {"error": f"bad skill name: {name!r}"}
            meta, body = _parse_frontmatter(content)
            if not meta or str(meta.get("name") or "").lower() != name:
                return {"error": "the SKILL.md frontmatter didn't follow the contract"}
            if not str(meta.get("description") or "").strip() or not body.strip():
                return {"error": "the SKILL.md needs a description and a body"}
            return {"type": kind, "name": name, "skill_md": content}
        return {"error": f"unknown draft type: {kind!r}"}
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


def install_workflow_skill(cfg, name: str, content: str) -> str:
    """Write an approved SKILL.md into skills_dir/<name>/ (backing up any
    previous version as .bak). Returns the path."""
    safe = os.path.basename(str(name))
    if not _COMMAND_RE.match(safe) or safe in _RESERVED:
        raise ValueError(f"bad skill name: {name!r}")
    directory = os.path.join(skills_dir_for(cfg), safe)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "SKILL.md")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            previous = f.read()
        with open(path + ".bak", "w", encoding="utf-8") as f:
            f.write(previous)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    log_diag("skills", f"installed workflow {safe} chars={len(content)}")
    return path
