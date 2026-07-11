"""Built-in skill: reminders.

/remind in 20m stretch
/remind at 5pm take out trash
/remind tomorrow 9am call the bank
/remind list
/remind cancel <id>

The tray daemon fires due reminders as desktop toasts (30s poll).
"""
SKILL = {"command": "remind",
         "description": "Set reminders: /remind in 20m stretch · at 5pm … · list · cancel <id>",
         "kind": "python"}


def run(args, ctx):
    from livingpc.reminders import ReminderStore, parse_when
    store = ReminderStore(ctx["memory_db"])
    try:
        text = (args or "").strip()
        if not text or text.lower() == "list":
            pending = store.pending()
            if not pending:
                return "No reminders set. Try `/remind in 20m stretch`."
            lines = [f"- #{r['id']} {r['due_ts'].replace('T', ' ')} — {r['text']}"
                     for r in pending]
            return "Pending reminders:\n" + "\n".join(lines)
        if text.lower().startswith("cancel"):
            raw = text[len("cancel"):].strip().lstrip("#")
            if not raw.isdigit():
                return "Which one? `/remind cancel <id>` (see `/remind list`)."
            return ("Cancelled." if store.cancel(int(raw))
                    else "No pending reminder with that id.")
        due, message = parse_when(text)
        if due is None:
            return ("I couldn't read the time. Formats: `in 20m`, `in 1h30m`, "
                    "`at 5pm`, `at 17:30`, `tomorrow 9am`.")
        if not message:
            return "Remind you about what? `/remind in 20m <message>`"
        reminder_id = store.add(due, message)
        nice = due.strftime("%a %H:%M")
        return (f"⏰ Set #{reminder_id} for {nice}: {message}\n"
                "(fires as a desktop toast while the tray daemon is running)")
    finally:
        store.close()
