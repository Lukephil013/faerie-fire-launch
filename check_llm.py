"""Check whether triage can reach the LLM.

    python check_llm.py

Verifies, in order: the anthropic package is installed, ANTHROPIC_API_KEY is set,
the configured model answers a tiny test call. Prints a clear pass/fail for each.
Costs a fraction of a cent if it reaches the API.
"""
from __future__ import annotations

import os

from livingpc.config import load


def main() -> None:
    cfg = load("config.toml")
    print(f"Configured backend: {cfg.llm_backend}")
    print(f"Configured model:   {cfg.llm_model}\n")

    if cfg.llm_backend != "claude":
        print(f"Backend is '{cfg.llm_backend}', not 'claude' — no LLM call is made.")
        print("Set llm_backend = \"claude\" in config.toml to use the cloud model.")
        return

    # 1) package
    try:
        from anthropic import Anthropic
    except Exception as e:
        print("[FAIL] anthropic package not installed.")
        print(f"       {type(e).__name__}: {e}")
        print("       -> python -m pip install anthropic")
        return
    print("[ok]   anthropic package installed")

    # 2) key
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("[FAIL] ANTHROPIC_API_KEY is not set in this environment.")
        print('       -> setx ANTHROPIC_API_KEY "sk-ant-..."  (then reopen the terminal)')
        return
    print(f"[ok]   ANTHROPIC_API_KEY is set (…{key[-4:]})")

    # 3) live call
    print("\nMaking a tiny test call...")
    try:
        client = Anthropic(
            api_key=key,
            timeout=min(getattr(cfg, "llm_timeout_seconds", 60.0), 30.0),
            max_retries=0,
        )
        msg = client.messages.create(
            model=cfg.llm_model,
            max_tokens=20,
            messages=[{"role": "user", "content": "Reply with exactly: connected"}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        print(f"[ok]   model replied: {text.strip()!r}")
        print("\nConnected. You can run real triage:  python run_triage.py")
    except Exception as e:
        print(f"[FAIL] API call failed: {type(e).__name__}: {e}")
        print("       If it's a model-name error, change llm_model in config.toml.")
        print("       If it's auth/credit, check the key and your account billing.")


if __name__ == "__main__":
    main()
