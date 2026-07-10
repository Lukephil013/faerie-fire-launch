"""First-run onboarding for the launch profile.

A launch-profile install starts with no memory.db, no goal tree, and no API
key. This module owns the three things that don't belong anywhere else:

  * storing the user's own Anthropic API key at rest (DPAPI-protected on
    Windows via livingpc.crypto, matching how the database encryption key is
    already protected; plaintext with restrictive permissions elsewhere, same
    compatibility stance crypto.py takes for the DB key),
  * validating a key with one live, minimal call before accepting it, and
  * a small on-disk marker for "onboarding finished" so the app doesn't ask
    again — deliberately not inferred from memory.db/tree state, since those
    get created as a side effect of merely opening a store to check.

Every module that talks to Anthropic reads `os.environ["ANTHROPIC_API_KEY"]`
as a fallback already (see feedback.py, curiosity.py, goal_ai.py, brain.py,
etc.), so populating that environment variable here at startup is the only
integration point needed — nothing else has to change to pick up a stored key.
"""
from __future__ import annotations

import os

from .config import DATA_DIR
from . import crypto

_KEY_FILE = os.path.join(DATA_DIR, "api_key.secret")
_MARKER_FILE = os.path.join(DATA_DIR, ".onboarding_complete")


def has_stored_key() -> bool:
    return os.path.exists(_KEY_FILE)


def load_api_key() -> str | None:
    """Read the stored key, if any, and return it in plaintext (does not touch env vars)."""
    if not os.path.exists(_KEY_FILE):
        return None
    try:
        with open(_KEY_FILE, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if crypto.dpapi_available():
        try:
            return crypto.unprotect_secret(raw).decode()
        except crypto.EncryptionError:
            return None
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return None


def apply_stored_key() -> bool:
    """Populate ANTHROPIC_API_KEY from storage if it isn't already set.

    Returns True if a key is available in the environment afterward (whether
    it was already set or was just loaded from storage).
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    key = load_api_key()
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
        return True
    return False


def save_api_key(key: str) -> None:
    """Persist the key at rest and set it for the current process."""
    key = (key or "").strip()
    if not key:
        raise ValueError("API key is empty")
    os.makedirs(DATA_DIR, exist_ok=True)
    if crypto.dpapi_available():
        protected = crypto.protect_secret(key.encode())
        with open(_KEY_FILE, "wb") as handle:
            handle.write(protected)
    else:
        with open(_KEY_FILE, "wb") as handle:
            handle.write(key.encode())
        try:
            os.chmod(_KEY_FILE, 0o600)
        except OSError:
            pass
    os.environ["ANTHROPIC_API_KEY"] = key


def validate_api_key(key: str) -> tuple[bool, str]:
    """One minimal live call to confirm the key actually works before we store it."""
    key = (key or "").strip()
    if not key:
        return False, "Enter an API key first."
    if not key.startswith("sk-ant-"):
        return False, "That doesn't look like an Anthropic API key (expected it to start with \"sk-ant-\")."
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=key, timeout=15.0)
        client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True, ""
    except Exception as error:  # noqa: BLE001 - surfaced to the user, not swallowed
        return False, _friendly_error(error)


def _friendly_error(error: Exception) -> str:
    text = str(error)
    lowered = text.lower()
    if "authentication" in lowered or "401" in text:
        return "That key was rejected by Anthropic (authentication failed)."
    if "credit" in lowered or "billing" in lowered or "402" in text:
        return "The key is valid, but that account has no available credit."
    return f"Could not validate the key: {type(error).__name__}: {error}"


def is_complete() -> bool:
    return os.path.exists(_MARKER_FILE)


def mark_complete() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_MARKER_FILE, "w") as handle:
        handle.write("1")


DEFAULT_INVESTIGATION_LABEL = "Getting to know Faerie Fire"
DEFAULT_INVESTIGATION_DIRECTIVE = (
    "This is a seeded starter investigation so there's something to look at on "
    "day one. Investigations are open questions Faerie actively pursues — "
    "asking you things, and once grounded in what you've confirmed, suggesting "
    "next moves. Answer a question below whenever you like, or start a real "
    "investigation of your own from the Investigations tab and archive this one."
)


def seed_example_investigation(memory_db_path: str) -> int | None:
    """Create the one starter investigation the launch plan calls for.

    Best-effort: onboarding should still complete even if this fails for some
    reason (e.g. schema not yet migrated), so callers should not treat a
    failure here as fatal.
    """
    try:
        from .curiosity import CuriosityStore
    except Exception:
        return None
    store = CuriosityStore(memory_db_path)
    try:
        return store.add_curiosity(DEFAULT_INVESTIGATION_DIRECTIVE, DEFAULT_INVESTIGATION_LABEL)
    except Exception:
        return None
    finally:
        store.close()
