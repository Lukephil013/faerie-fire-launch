"""Phase 2 — the real-time assistant core.

Pure-ish: builds the multimodal message (current screen image + redacted on-screen
text + what the brain knows about you + your question) and asks Claude. The GUI
(assistant.py) handles the hotkey, screen capture, and display.
"""
from __future__ import annotations

import base64
import io
import os

from .diagnostics import log_diag
from .memory_context import estimate_tokens, format_memories, select_memories

SYSTEM_PROMPT = """\
You are Faerie Fire — a sharp, warm, real-time assistant who already knows this
person from their personal "second brain." They've pressed a hotkey mid-activity
and asked you something. You can see their current screen (an image) and you have
notes about who they are and what they care about.

How to answer:
- Lead with the answer. Be concise and concrete — they're in the middle of something.
- Use what you know about them to tailor it; if their memory is relevant, lean on it.
- If it's a game (e.g. League of Legends), give specific, practical, in-the-moment
  advice (builds, matchups, what to do next) based on what's on screen.
- If it's studying (e.g. Korean), be a helpful tutor: explain, quiz, or expand.
- Don't narrate everything you see unless asked; answer the actual question.
- If you're missing what you'd need, say so briefly and give your best guess anyway.
"""


def encode_jpeg_b64(pil_image, quality: int = 70, max_width: int = 1400) -> str:
    """PIL image -> base64 JPEG string (downscaled to keep tokens/cost sane)."""
    img = pil_image
    if img.width > max_width:
        h = int(img.height * max_width / img.width)
        img = img.resize((max_width, h))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def build_content(question: str, image_b64: str | None,
                  ocr_text: str, memories: list[dict], *,
                  memory_max_items: int = 20, memory_max_chars: int = 6000,
                  memory_value_max_chars: int = 500) -> list:
    """Assemble the user message content blocks for the Anthropic API."""
    blocks = []
    if image_b64:
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
        })
    ctx = []
    if memories:
        selection = select_memories(
            memories,
            question + "\n" + ocr_text,
            max_items=memory_max_items,
            max_chars=memory_max_chars,
            value_max_chars=memory_value_max_chars,
        )
        ctx.append("WHAT I KNOW ABOUT YOU (relevant memory):\n" +
                   format_memories(selection.memories))
        log_diag(
            "prompt",
            f"surface=assistant memories={len(selection.memories)}/{len(memories)} "
            f"memory_chars={selection.selected_chars}/{selection.full_chars} "
            f"estimated_memory_tokens={selection.estimated_tokens}",
        )
    if ocr_text:
        ctx.append("ON-SCREEN TEXT (redacted, may be noisy):\n" + ocr_text[:3000])
    ctx.append("MY QUESTION:\n" + question)
    blocks.append({"type": "text", "text": "\n\n".join(ctx)})
    return blocks


def answer(question: str, image_b64: str | None, ocr_text: str,
           memories: list[dict], model: str = "claude-sonnet-4-6",
           api_key: str | None = None, max_tokens: int = 700, *,
           memory_max_items: int = 20, memory_max_chars: int = 6000,
           memory_value_max_chars: int = 500) -> str:
    """Call Claude with the multimodal context and return the text answer."""
    from anthropic import Anthropic

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set.")
    client = Anthropic(api_key=key)
    content = build_content(
        question,
        image_b64,
        ocr_text,
        memories,
        memory_max_items=memory_max_items,
        memory_max_chars=memory_max_chars,
        memory_value_max_chars=memory_value_max_chars,
    )
    text_chars = len(SYSTEM_PROMPT) + sum(
        len(block.get("text", "")) for block in content if block.get("type") == "text"
    )
    log_diag(
        "prompt",
        f"surface=assistant text_input_chars={text_chars} "
        f"estimated_text_tokens={estimate_tokens(text_chars)} image_present={bool(image_b64)}",
    )
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
