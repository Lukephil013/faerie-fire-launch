"""Built-in skill: /briefing — one place to start the day.

Pending reminders, open curiosities/goals, and your freshest project docs,
stitched into a short morning note by the chat model. Each section is
best-effort: a missing subsystem just drops out instead of failing.
"""
SKILL = {"command": "briefing",
         "description": "Morning briefing: reminders due, open goals, fresh project docs",
         "kind": "python"}


def run(args, ctx):
    import os
    sections = []

    try:
        from livingpc.reminders import ReminderStore
        store = ReminderStore(ctx["memory_db"])
        try:
            pending = store.pending()[:8]
        finally:
            store.close()
        if pending:
            sections.append("REMINDERS PENDING:\n" + "\n".join(
                f"- {r['due_ts'].replace('T', ' ')} — {r['text']}" for r in pending))
    except Exception:
        pass

    try:
        from livingpc.curiosity import CuriosityStore
        store = CuriosityStore(ctx["memory_db"])
        try:
            rows = [r for r in store.list_curiosities() if r["status"] == "active"][:6]
            if rows:
                sections.append("ACTIVE GOALS / CURIOSITIES:\n" + "\n".join(
                    f"- {r['label']}: {r['directive']}" for r in rows))
        finally:
            store.close()
    except Exception:
        pass

    try:
        from livingpc import filing
        projects_dir = filing.projects_dir_for(ctx["cfg"])
        docs = filing.build_catalog(projects_dir)
        docs.sort(key=lambda d: os.path.getmtime(d["path"]), reverse=True)
        if docs:
            sections.append("FRESHEST PROJECT DOCS:\n" + "\n".join(
                f"- {d['title']}" + (f" — {d['summary'][:80]}" if d["summary"] else "")
                for d in docs[:5]))
    except Exception:
        pass

    if not sections:
        return ("Nothing on the radar: no reminders, goals, or project docs yet. "
                "`/remind` and `/file` are how they get here.")
    return ctx["llm"](
        "Turn this raw status into a short, warm morning briefing for the "
        "user: one tight paragraph plus, only if reminders exist, a short "
        "list of them with times. No headers, no filler, no 'here is your "
        "briefing'.",
        "\n\n".join(sections))
