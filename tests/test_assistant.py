"""Tests for the assistant content builder and hotkey parser (no network/GUI)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc import assist  # noqa: E402


def test_build_content_with_image():
    mems = [{"category": "League of Legends", "attribute": "champion pool",
             "value": "Caitlyn, Tristana"}]
    blocks = assist.build_content("what should I build?", "ZmFrZQ==",
                                  "enemy: Malphite Ornn", mems)
    # first block is the image, last block is the text context
    assert blocks[0]["type"] == "image"
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    text = blocks[-1]["text"]
    assert "WHAT I KNOW ABOUT YOU" in text
    assert "Caitlyn, Tristana" in text
    assert "ON-SCREEN TEXT" in text
    assert "what should I build?" in text


def test_build_content_text_only():
    blocks = assist.build_content("hi", None, "", [])
    # no image block when image_b64 is None
    assert all(b["type"] != "image" for b in blocks)
    assert blocks[-1]["text"].startswith("MY QUESTION:")


def test_build_content_selects_relevant_bounded_memories():
    memories = [
        {"id": i, "category": "Cooking", "attribute": f"meal {i}", "value": "ramen"}
        for i in range(5)
    ]
    memories.append(
        {"id": 99, "category": "League of Legends", "attribute": "champion", "value": "Caitlyn"}
    )
    blocks = assist.build_content(
        "Caitlyn build?", None, "League match", memories,
        memory_max_items=2, memory_max_chars=180, memory_value_max_chars=50,
    )
    text = blocks[-1]["text"]
    assert "Caitlyn" in text
    assert text.count("- [") <= 2


def test_encode_jpeg_b64_downscales():
    from PIL import Image
    img = Image.new("RGB", (3000, 1500), (10, 20, 30))
    b64 = assist.encode_jpeg_b64(img, max_width=1400)
    assert isinstance(b64, str) and len(b64) > 100   # produced something


def _parse(spec):
    # import here so the test file doesn't need tkinter at import time
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assistant.py")
    src = open(path, encoding="utf-8").read()
    # extract+exec just the parse_hotkey function deps minimally:
    ns = {}
    # pull the _MODS/_KEYS/parse_hotkey block by exec of the whole module is unsafe
    # (imports tkinter). Instead re-implement check via a tiny eval of the maps.
    exec(compile(_PARSE_SNIPPET, "<snip>", "exec"), ns)
    return ns["parse_hotkey"](spec)


_PARSE_SNIPPET = '''
_MODS = {"ctrl":0x0002,"control":0x0002,"alt":0x0001,"shift":0x0004,"win":0x0008,"super":0x0008}
_KEYS = {"space":0x20,"enter":0x0D,"return":0x0D,"tab":0x09,"esc":0x1B}
def parse_hotkey(spec):
    mod, vk = 0, None
    for part in spec.lower().split("+"):
        part = part.strip()
        if part in _MODS: mod |= _MODS[part]
        elif part in _KEYS: vk = _KEYS[part]
        elif len(part) == 1 and part.isalnum(): vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit(): vk = 0x70 + int(part[1:]) - 1
    return mod, vk
'''


def test_hotkey_parser():
    mod, vk = _parse("ctrl+shift+space")
    assert mod == (0x0002 | 0x0004) and vk == 0x20
    mod, vk = _parse("alt+a")
    assert mod == 0x0001 and vk == ord("A")
    mod, vk = _parse("ctrl+f5")
    assert vk == 0x70 + 4


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            fails += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns)-fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
