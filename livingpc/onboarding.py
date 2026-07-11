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
from .lang import T

_KEY_FILE = os.path.join(DATA_DIR, "api_key.secret")
_MARKER_FILE = os.path.join(DATA_DIR, ".onboarding_complete")


def has_stored_key() -> bool:
    # The file existing isn't enough: it's DPAPI-encrypted per user+machine,
    # so a data/ folder copied from another PC contains a key file that can't
    # decrypt here. Only report a key if it actually loads, so onboarding /
    # the Change API Key flow correctly ask for a new one on a new machine.
    return os.path.exists(_KEY_FILE) and load_api_key() is not None


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
        return False, T("Enter an API key first.", "먼저 API 키를 입력해주세요.")
    if not key.startswith("sk-ant-"):
        return False, T("That doesn't look like an Anthropic API key (expected it to start with \"sk-ant-\").",
                        "Anthropic API 키처럼 보이지 않아요. \"sk-ant-\"로 시작해야 해요.")
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
        return T("That key was rejected by Anthropic (authentication failed).",
                 "Anthropic에서 이 키를 거절했어요. 인증에 실패했습니다.")
    if "credit" in lowered or "billing" in lowered or "402" in text:
        return T("The key is valid, but that account has no available credit.",
                 "키는 유효하지만, 해당 계정에 사용 가능한 크레딧이 없어요.")
    return T(f"Could not validate the key: {type(error).__name__}: {error}",
             f"키를 확인할 수 없어요: {type(error).__name__}: {error}")


def is_complete() -> bool:
    return os.path.exists(_MARKER_FILE)


def mark_complete() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(_MARKER_FILE, "w") as handle:
        handle.write("1")


# Evaluated lazily (functions, not constants) because the language is chosen
# during onboarding itself, in the same process, right before seeding runs.
def default_investigation_label() -> str:
    return T("Getting to know Faerie Fire", "페어리 파이어 알아가기")


def default_investigation_directive() -> str:
    return T(
        "This is a seeded starter investigation so there's something to look at on "
        "day one. Investigations are open questions Faerie actively pursues — "
        "asking you things, and once grounded in what you've confirmed, suggesting "
        "next moves. Answer a question below whenever you like, or start a real "
        "investigation of your own from the Investigations tab and archive this one.",
        "첫날 바로 살펴볼 수 있도록 미리 심어둔 시작 탐구예요. 탐구는 페어리가 "
        "계속 따라가며 질문하고, 당신이 확인해준 내용을 바탕으로 다음 움직임을 "
        "제안하는 열린 질문이에요. 원할 때 아래 질문에 답하거나, 채팅에서 새로운 "
        "탐구를 시작한 뒤 이 항목을 보관해도 좋아요.",
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
        return store.add_curiosity(default_investigation_directive(), default_investigation_label())
    except Exception:
        return None
    finally:
        store.close()
