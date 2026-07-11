# Faerie Fire launchers

**First time:** run **Install Dependencies.bat**, then **Setup Background
Capture.bat** to start capture and register it to run at Windows login.

Everyday use:

- **Living Computer.bat** — one double-click entry point for the full app; starts
  the tray daemon, whose left-click opens the Review GUI.
- **Capture Control.bat** — the main panel: start/stop/status, logs, diagnostics,
  a safe **Reset**, and an emergency Force-Stop.
- **Start Background Capture.bat** / **Stop Background Capture.bat** — start or
  stop the always-on capture (with its tray icon) directly.
- **Memory GUI.bat**, **Companion.bat**, **Ask Assistant.bat** — open the apps.
  The GUI has two tabs: Inferences (Yes/No review of hypotheses) and Memory. The
  nightly pass (triage + inference + backups) runs in the background daemon.

Knowledge tools:

- **Backfill Inferences.bat** — seed inference evidence from already-captured
  history and clear pre-rework rows. Stop capture first.

Setup / maintenance:

- **Reset Capture.bat** — safely restart capture when it gets stuck (targets only
  this project's processes; narrower than the Force-Stop in Capture Control).
- **Collect Diagnostics.bat** / **Collect Companion Diagnostics.bat** — privacy-safe
  troubleshooting bundles (metadata only).
- **Git Setup.bat** / **Git Push.bat** — initialize the repo / commit and push.

These files compute the project directory from their own location, so the folder
can be moved or renamed without editing paths. Re-run **Setup Background
Capture.bat** after moving the whole project so the login task gets the new path.
