"""Minimal .docx text extraction — no dependencies.

A .docx is a zip; the document body lives in word/document.xml. We pull the
text runs (<w:t>), joining paragraphs (<w:p>) with newlines and honoring line
breaks (<w:br>) and tabs, which is all the journal importer needs. Formatting,
tables-as-layout, images, headers/footers are intentionally ignored.

Legacy binary .doc is NOT supported (proprietary format; would need a real
dependency) — callers should tell the user to save as .docx.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_to_text(data: bytes) -> str:
    """Extract plain text from .docx bytes. Raises ValueError if not a docx."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_bytes = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as error:
        raise ValueError(f"not a .docx file: {error}") from error
    root = ET.fromstring(xml_bytes)
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}br":
                parts.append("\n")
            elif node.tag == f"{_W}tab":
                parts.append("\t")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs).strip()
