from html.parser import HTMLParser
from pathlib import Path


PREVIEW = Path(__file__).resolve().parents[1] / "livingpc" / "ui" / "command_center_preview.html"


class _PreviewParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag in {"img", "script"} and values.get("src"):
            self.sources.append(values["src"])


def test_command_center_preview_is_static_and_complete():
    text = PREVIEW.read_text(encoding="utf-8")
    parser = _PreviewParser()
    parser.feed(text)

    assert {"preview-chat", "preview-composer", "preview-input"} <= parser.ids
    assert "pywebview.api" not in text
    assert "fetch(" not in text
    assert "XMLHttpRequest" not in text
    assert "https://" not in text and "http://" not in text
    assert not parser.sources


def test_command_center_preview_local_assets_exist():
    ui = PREVIEW.parent
    for relative in (
        "assets/fonts/AtkinsonHyperlegible-Regular.ttf",
        "assets/fonts/AtkinsonHyperlegible-Bold.ttf",
        "assets/backgrounds/forest-ruins-main.jpg",
    ):
        assert (ui / relative).is_file(), relative
