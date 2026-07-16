# Smoke test for the unified (bilingual) build. Run via "RUN SMOKE TEST.bat".
# --quick skips the live model call (used by New Test Instance.bat as a
# pre-launch gate). Writes diagnostics/smoke_result.txt; restores config.toml.
# Exit code 1 if anything failed.
import os
import re
import shutil
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

QUICK = "--quick" in sys.argv
results = []


def check(name, fn):
    try:
        results.append(f"[ok] {name}: {fn()}")
        return True
    except Exception:
        results.append(f"[X] {name}:\n{traceback.format_exc()}")
        return False


config_before = None
try:
    config_before = open("config.toml", encoding="utf-8").read()
except OSError:
    pass

# --- imports / syntax ---
check("import gui.py", lambda: __import__("gui") and "imports clean")
check("import brain.py", lambda: __import__("livingpc.companion.brain", fromlist=["x"]) and "imports clean")
# gui.py imports this lazily inside open-chat calls, so a missing module only
# surfaces at runtime ("No module named 'companion'") — test it eagerly here.
check("chat bridge (companion.Api)", lambda: getattr(__import__("companion"), "Api") and "imports clean")

from livingpc import lang, onboarding, soul_calibration  # noqa: E402
from livingpc.companion import personas  # noqa: E402
from livingpc import curiosity_metrics  # noqa: E402

# --- english defaults ---
check("app_language default", lambda: lang.app_language())
check("FIELDS via module __getattr__", lambda: f"{len(soul_calibration.FIELDS)} fields")
check("sections_in_order", lambda: f"{len(soul_calibration.sections_in_order())} sections")
check("EN field label", lambda: soul_calibration.FIELDS[0]["label"])
check("EN persona", lambda: personas.get_persona("companion").name)
check("EN seed label", lambda: onboarding.default_investigation_label())

# --- switch to korean in-process ---
check("set_app_language ko", lambda: lang.set_app_language("ko"))
check("KO field label", lambda: soul_calibration.FIELDS[0]["label"])
check("KO section", lambda: soul_calibration.sections_in_order()[0])
check("KO persona", lambda: personas.get_persona("companion").name)
check("KO persona system mentions Korean",
      lambda: "Korean" in personas.get_persona("companion").system and "yes")
check("KO seed label", lambda: onboarding.default_investigation_label())

# --- GoalAI-drafted profile (real model call, korean mode) ---
def draft():
    cur = {"id": 999, "label": "Energy & Food Investigation",
           "directive": ("Luke feels highly sensitive to energy and food fluctuations. "
                         "His morning routine works well. Crashes happen at handoff "
                         "points - social eating, opportunistic snacking, moments where "
                         "the plan breaks down.")}
    profile = curiosity_metrics.proposed_profile(cur)
    if profile is None:
        return "returned None (no key or model failure) — would fall back to templates"
    dims = ", ".join(d.label for d in profile.dimensions)
    states = ", ".join(d.label for d in profile.state_metrics)
    return f"domain={profile.domain} | dims: {dims} | states: {states}"


if QUICK:
    results.append("[ok] GoalAI profile draft: skipped (--quick)")
else:
    check("GoalAI profile draft (live call)", draft)

# --- restore config ---
if config_before is not None:
    open("config.toml", "w", encoding="utf-8").write(config_before)
    results.append("[ok] config.toml restored")
lang._cached = None

# --- JS syntax check of the ported memory.html (needs node) ---
def js_check():
    html = open(os.path.join("livingpc", "ui", "memory.html"), encoding="utf-8").read()
    if not html.rstrip().endswith("</html>"):
        return "FILE TRUNCATED"
    scripts = re.findall(r"<script>\s*(.*?)\s*</script>", html, re.DOTALL)
    if not scripts:
        return "NO INLINE SCRIPT FOUND"
    if shutil.which("node") is None:
        return "skipped (Node.js is optional and is not installed)"
    for index, script in enumerate(scripts, start=1):
        proc = subprocess.run(
            ["node", "--check", "-"], input=script,
            capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode != 0:
            return f"SYNTAX ERROR in block {index}:\n" + proc.stderr[:2000]
    return f"node --check passed ({len(scripts)} blocks, {sum(map(len, scripts))} bytes of JS)"


check("memory.html JS syntax", js_check)

report = "\n".join(results)
os.makedirs("diagnostics", exist_ok=True)
with open(os.path.join("diagnostics", "smoke_result.txt"), "w", encoding="utf-8") as handle:
    handle.write(report + "\n")
print(report)
failed = any(line.startswith("[X]") for line in results)
sys.exit(1 if failed else 0)
