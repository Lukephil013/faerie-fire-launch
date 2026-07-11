"""Desktop notifications — dependency-free Windows toasts.

Uses the WinRT toast API through PowerShell (works on Win10/11 with no Python
package), fired-and-forgotten in a hidden subprocess so a slow shell can never
block a caller. On non-Windows (and in tests) it just logs. Best-effort by
design: a failed toast must never break an import or the scheduler.

Callers: the Memory GUI (import finished), the inference scheduler
(nightly "N inferences waiting" reminder + a heads-up when a new hypothesis
crosses the confidence gate). Disable globally with notifications_enabled=false.
"""
from __future__ import annotations

import os
import subprocess

from .diagnostics import log_diag

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_PS_TEMPLATE = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode($env:FF_TITLE)) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode($env:FF_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Faerie Fire').Show($toast)
"""


def notify(title: str, message: str, *, cfg=None) -> bool:
    """Show a desktop toast. Returns True if one was dispatched."""
    if cfg is not None and not getattr(cfg, "notifications_enabled", True):
        return False
    title = str(title or "Faerie Fire")[:120]
    message = str(message or "")[:240]
    log_diag("notify", f"toast title_chars={len(title)} body_chars={len(message)}")
    if os.name != "nt":
        return False
    try:
        env = dict(os.environ, FF_TITLE=title, FF_BODY=message)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle",
             "Hidden", "-Command", _PS_TEMPLATE],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        return True
    except Exception as error:
        log_diag("notify", f"toast failed error={type(error).__name__}: {error}")
        return False


def review_reminder(count: int) -> tuple[str, str]:
    """The daily nudge (pure, for tests)."""
    noun = "inference" if count == 1 else "inferences"
    return (f"{count} {noun} ready for review",
            "Open the Memory GUI — Yes / Kind of / No, or teach it why it's wrong.")


def import_summary(stats: dict, dry_run: bool = False) -> tuple[str, str]:
    """Toast content for a finished journal import (pure, for tests)."""
    added = stats.get("added", 0)
    superseded = stats.get("superseded", 0)
    return ("Journal import finished",
            f"+{added} fact(s), {superseded} superseded. "
            "Check the Timeline tab.")
