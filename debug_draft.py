# Debug the GoalAI profile draft with full tracebacks. Run via RUN DEBUG DRAFT.bat.
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

out = []
out.append("key present: " + str(bool(os.environ.get("ANTHROPIC_API_KEY"))))
try:
    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"), timeout=25.0, max_retries=0)
    model = os.environ.get("FAERIE_METRIC_MODEL") or "claude-sonnet-4-6"
    out.append("model: " + model)
    msg = client.messages.create(
        model=model, max_tokens=900,
        system="Reply with STRICT JSON only: {\"dimensions\":[{\"slug\":\"a\",\"label\":\"b\",\"description\":\"c\",\"checkin_prompt\":\"d\"}],\"state_metrics\":[{\"slug\":\"e\",\"label\":\"f\",\"description\":\"g\",\"checkin_prompt\":\"h\"}]}",
        messages=[{"role": "user", "content": "test"}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    out.append("raw call ok, response head: " + text[:200])
except Exception:
    out.append("RAW CALL FAILED:\n" + traceback.format_exc())

try:
    from livingpc import curiosity_metrics as cm
    cur = {"id": 999, "label": "Energy & Food Investigation",
           "directive": "Sensitive to energy and food fluctuations; crashes at handoff points."}
    # call the internal function but with exceptions surfaced
    key = os.environ.get("ANTHROPIC_API_KEY")
    from anthropic import Anthropic
    from livingpc.lang import is_ko
    client = Anthropic(api_key=key, timeout=25.0, max_retries=0)
    model = os.environ.get("FAERIE_METRIC_MODEL") or "claude-sonnet-4-6"
    system = cm._DRAFT_SYSTEM
    prompt = "INVESTIGATION TITLE: " + cur["label"] + "\nFRAMING:\n" + cur["directive"]
    msg = client.messages.create(model=model, max_tokens=900, system=system,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    out.append("draft raw response:\n" + text[:1500])
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])
    out.append("json parsed: dims=%d states=%d" % (
        len(data.get("dimensions") or []), len(data.get("state_metrics") or [])))
    profile = cm.proposed_profile(cur)
    out.append("proposed_profile: " + (
        f"domain={profile.domain} dims={[d.label for d in profile.dimensions]} "
        f"states={[d.label for d in profile.state_metrics]}" if profile else "None"))
except Exception:
    out.append("DRAFT PATH FAILED:\n" + traceback.format_exc())

report = "\n\n".join(out)
open("draft_debug.txt", "w", encoding="utf-8").write(report)
print(report)
