"""Built-in skill: /today — what did I do today?

Aggregates the capture log for a date (default today), redacts it like triage
does, and asks the chat model for a short human recap. `/today 2026-07-07`
works too. Costs one model call.
"""
SKILL = {"command": "today",
         "description": "Recap a day's activity from the capture log: /today [YYYY-MM-DD]",
         "kind": "python"}


def run(args, ctx):
    import re
    from datetime import date
    from livingpc.storage import EventLog
    from livingpc.triage.aggregate import build_day_summary
    from livingpc.triage.redact import redact

    day = (args or "").strip() or date.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return "Give me a date like `/today 2026-07-07`, or nothing for today."
    store = EventLog(ctx["cfg"].db_path)
    try:
        summary = build_day_summary(store, day)
    finally:
        store.close()
    if "## " not in (summary or ""):   # header only, no per-app sections
        return f"No captured activity for {day} — was capture running?"
    summary = redact(summary)[:12000]
    return ctx["llm"](
        "You summarize one day of the user's computer activity into a short, "
        "warm recap: 4-6 sentences, concrete (apps, focus blocks, what they "
        "worked on), no bullet points, no moralizing about screen time.",
        f"Activity log for {day}:\n\n{summary}")
