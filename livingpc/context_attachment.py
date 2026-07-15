"""Encrypted, owner-scoped document context for reflection surfaces.

Attachments are extracted locally and stored as encrypted text in memory.db.
Raw files are not copied. Owners are opaque stable keys such as one Soul
Calibration field, one Investigation, or one Investigation question.
"""
from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone

from . import crypto
from .db import connect


ALLOWED_OWNER_KINDS = {"soul_calibration", "curiosity", "curiosity_item"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm"}
MAX_FILE_BYTES = 15_000_000
MAX_EXTRACTED_CHARS = 200_000
MAX_ATTACHMENTS_PER_OWNER = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS context_attachment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_kind TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content_text TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (owner_kind IN ('soul_calibration','curiosity','curiosity_item')),
    UNIQUE (owner_kind,owner_key,content_sha256)
);
CREATE INDEX IF NOT EXISTS idx_context_attachment_owner
ON context_attachment(owner_kind,owner_key,id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_owner(owner_kind: str, owner_key) -> tuple[str, str]:
    kind = str(owner_kind or "").strip()
    key = str(owner_key or "").strip()
    if kind not in ALLOWED_OWNER_KINDS:
        raise ValueError("unsupported attachment owner")
    if not key or len(key) > 240:
        raise ValueError("invalid attachment owner key")
    return kind, key


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference or "")
    if not letters:
        return 0
    value = 0
    for letter in letters.group(0).upper():
        value = value * 26 + ord(letter) - 64
    return max(0, value - 1)


def _xlsx_to_text(data: bytes) -> str:
    """Dependency-free extraction of displayed cell values from XLSX/XLSM."""
    spreadsheet = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    office_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    package_rel = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ValueError("could not read that Excel workbook") from error
    with archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.iter(spreadsheet + "si")]

        sheets: list[tuple[str, str]] = []
        try:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {
                node.attrib.get("Id", ""): node.attrib.get("Target", "")
                for node in relationships.iter(package_rel)
            }
            for sheet in workbook.iter(spreadsheet + "sheet"):
                target = targets.get(sheet.attrib.get(office_rel, ""), "")
                if not target:
                    continue
                path = (posixpath.normpath(target.lstrip("/")) if target.startswith("/")
                        else posixpath.normpath(posixpath.join("xl", target)))
                if path in names:
                    sheets.append((sheet.attrib.get("name", "Sheet"), path))
        except (KeyError, ET.ParseError):
            pass
        if not sheets:
            sheets = [(f"Sheet {index + 1}", name) for index, name in enumerate(
                      sorted(name for name in names
                             if name.startswith("xl/worksheets/") and name.endswith(".xml")))]

        rendered: list[str] = []
        for sheet_name, path in sheets:
            try:
                root = ET.fromstring(archive.read(path))
            except (KeyError, ET.ParseError):
                continue
            rows: list[str] = []
            for row in root.iter(spreadsheet + "row"):
                values: dict[int, str] = {}
                for cell in row.findall(spreadsheet + "c"):
                    column = _column_index(cell.attrib.get("r", ""))
                    kind = cell.attrib.get("t", "")
                    if kind == "inlineStr":
                        value = "".join(cell.itertext())
                    else:
                        node = cell.find(spreadsheet + "v")
                        value = node.text if node is not None and node.text is not None else ""
                        if kind == "s" and value:
                            try:
                                value = shared[int(value)]
                            except (ValueError, IndexError):
                                pass
                        elif kind == "b":
                            value = "TRUE" if value == "1" else "FALSE"
                    values[column] = str(value or "").replace("\r", " ").replace("\n", " ")
                if values:
                    last = min(max(values), 255)
                    rows.append("\t".join(values.get(index, "") for index in range(last + 1)).rstrip())
            if rows:
                rendered.append(f"[Sheet: {sheet_name}]\n" + "\n".join(rows))
        return "\n\n".join(rendered).strip()


def _legacy_doc_to_text(path: str) -> str:
    """Read binary .doc through local Microsoft Word when available."""
    try:
        import pythoncom
        import win32com.client
    except ImportError as error:
        raise ValueError("legacy DOC support needs Microsoft Word; save it as DOCX") from error
    pythoncom.CoInitialize()
    word = document = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        document = word.Documents.Open(
            FileName=os.path.abspath(path), ConfirmConversions=False,
            ReadOnly=True, AddToRecentFiles=False, Visible=False, OpenAndRepair=True)
        return str(document.Content.Text or "")
    except Exception as error:
        raise ValueError("could not read that DOC file; save it as DOCX") from error
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def _legacy_xls_to_text(path: str) -> str:
    """Read binary .xls through local Microsoft Excel when available."""
    try:
        import pythoncom
        import win32com.client
    except ImportError as error:
        raise ValueError("legacy XLS support needs Microsoft Excel; save it as XLSX") from error
    pythoncom.CoInitialize()
    excel = workbook = None
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        workbook = excel.Workbooks.Open(os.path.abspath(path), ReadOnly=True)
        output: list[str] = []
        for sheet in workbook.Worksheets:
            raw = sheet.UsedRange.Value
            rows = raw if isinstance(raw, tuple) else ((raw,),)
            lines = []
            for row in rows:
                cells = row if isinstance(row, tuple) else (row,)
                lines.append("\t".join("" if value is None else str(value)
                                       for value in cells).rstrip())
            if any(line for line in lines):
                output.append(f"[Sheet: {sheet.Name}]\n" + "\n".join(lines))
        return "\n\n".join(output).strip()
    except Exception as error:
        raise ValueError("could not read that XLS file; save it as XLSX") from error
    finally:
        if workbook is not None:
            try:
                workbook.Close(False)
            except Exception:
                pass
        if excel is not None:
            try:
                excel.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def extract_document(path: str) -> dict:
    """Extract supported document text locally; never retain the raw bytes."""
    name = os.path.basename(str(path or ""))
    ext = os.path.splitext(name)[1].lower()
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise ValueError("document is too large (15 MB maximum)")
    with open(path, "rb") as handle:
        data = handle.read()
    if ext == ".docx":
        from .docx_text import docx_to_text
        text = docx_to_text(data)
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif ext == ".doc":
        text = _legacy_doc_to_text(path)
        media_type = "application/msword"
    elif ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise ValueError("PDF support is not installed") from error
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages)
        except Exception as error:
            raise ValueError("could not read that PDF") from error
        media_type = "application/pdf"
    elif ext in EXCEL_EXTENSIONS:
        text = _xlsx_to_text(data)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif ext == ".xls":
        text = _legacy_xls_to_text(path)
        media_type = "application/vnd.ms-excel"
    elif ext in TEXT_EXTENSIONS:
        if b"\x00" in data[:4096]:
            raise ValueError("document appears to be binary")
        text = _decode_text(data)
        media_type = "text/csv" if ext in {".csv", ".tsv"} else "text/plain"
    else:
        raise ValueError(
            "attach a DOC, DOCX, PDF, CSV, TSV, XLS, XLSX, Markdown, text, JSON, or log file")
    text = str(text or "").replace("\x00", "").strip()
    if not text:
        raise ValueError("no readable text was found in that document")
    truncated = len(text) > MAX_EXTRACTED_CHARS
    text = text[:MAX_EXTRACTED_CHARS]
    return {"name": name[:240], "media_type": media_type, "text": text,
            "char_count": len(text), "truncated": truncated}


def _tokens(value: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[^\W_]+", str(value or ""), re.UNICODE)
            if len(token) > 1}


def _chunks(text: str, size: int = 1800) -> list[str]:
    chunks = []
    for paragraph in re.split(r"\n\s*\n+", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for start in range(0, len(paragraph), size):
            chunks.append(paragraph[start:start + size])
    return chunks or [text[:size]]


def select_excerpt(text: str, query: str, max_chars: int) -> str:
    """Keep the opening plus locally ranked relevant chunks within a budget."""
    parts = _chunks(text)
    wanted = _tokens(query)
    ranked = []
    for index, part in enumerate(parts):
        overlap = len(_tokens(part) & wanted)
        ranked.append((overlap, 1 if index == 0 else 0, -index, index, part))
    selected = []
    used = 0
    for *_score, index, part in sorted(ranked, reverse=True):
        if index in {item[0] for item in selected}:
            continue
        remaining = max_chars - used
        if remaining <= 80:
            break
        excerpt = part[:remaining]
        selected.append((index, excerpt))
        used += len(excerpt) + 2
    return "\n\n".join(part for _index, part in sorted(selected))


class ContextAttachmentStore:
    def __init__(self, db_path: str):
        self.conn = connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _metadata(row) -> dict:
        return {"id": int(row["id"]), "owner_kind": row["owner_kind"],
                "owner_key": row["owner_key"],
                "name": crypto.dec(row["filename"]) or "document",
                "media_type": row["media_type"], "char_count": int(row["char_count"]),
                "created_at": row["created_at"]}

    def list(self, owner_kind: str, owner_key) -> list[dict]:
        kind, key = _clean_owner(owner_kind, owner_key)
        rows = self.conn.execute(
            "SELECT * FROM context_attachment WHERE owner_kind=? AND owner_key=? ORDER BY id",
            (kind, key)).fetchall()
        return [self._metadata(row) for row in rows]

    def add_text(self, owner_kind: str, owner_key, name: str, text: str,
                 media_type: str = "text/plain") -> dict:
        kind, key = _clean_owner(owner_kind, owner_key)
        content = str(text or "").strip()[:MAX_EXTRACTED_CHARS]
        if not content:
            raise ValueError("attachment text is empty")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM context_attachment WHERE owner_kind=? AND owner_key=?",
            (kind, key)).fetchone()[0]
        if int(count) >= MAX_ATTACHMENTS_PER_OWNER:
            raise ValueError(f"up to {MAX_ATTACHMENTS_PER_OWNER} documents can be attached here")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO context_attachment "
            "(owner_kind,owner_key,filename,media_type,content_text,content_sha256,char_count,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (kind, key, crypto.enc(str(name or "document")[:240]), str(media_type or "text/plain"),
             crypto.enc(content), digest, len(content), _now()))
        self.conn.commit()
        row = (self.conn.execute("SELECT * FROM context_attachment WHERE id=?", (cur.lastrowid,)).fetchone()
               if cur.lastrowid else self.conn.execute(
                   "SELECT * FROM context_attachment WHERE owner_kind=? AND owner_key=? AND content_sha256=?",
                   (kind, key, digest)).fetchone())
        result = self._metadata(row)
        result["deduped"] = not bool(cur.rowcount)
        return result

    def add_document(self, owner_kind: str, owner_key, path: str) -> dict:
        extracted = extract_document(path)
        result = self.add_text(owner_kind, owner_key, extracted["name"], extracted["text"],
                               extracted["media_type"])
        result["truncated"] = extracted["truncated"]
        return result

    def remove(self, attachment_id: int, owner_kind: str, owner_key) -> bool:
        kind, key = _clean_owner(owner_kind, owner_key)
        cur = self.conn.execute(
            "DELETE FROM context_attachment WHERE id=? AND owner_kind=? AND owner_key=?",
            (int(attachment_id), kind, key))
        self.conn.commit()
        return bool(cur.rowcount)

    def clear_kind(self, owner_kind: str) -> int:
        kind, _ = _clean_owner(owner_kind, "all")
        cur = self.conn.execute("DELETE FROM context_attachment WHERE owner_kind=?", (kind,))
        self.conn.commit()
        return int(cur.rowcount)

    def context_block(self, owners: list[tuple[str, object]], *, query: str = "",
                      max_chars: int = 16000) -> str:
        rows = []
        seen = set()
        for owner_kind, owner_key in owners:
            kind, key = _clean_owner(owner_kind, owner_key)
            for row in self.conn.execute(
                    "SELECT * FROM context_attachment WHERE owner_kind=? AND owner_key=? ORDER BY id",
                    (kind, key)).fetchall():
                if int(row["id"]) not in seen:
                    rows.append(row); seen.add(int(row["id"]))
        if not rows:
            return "  (none attached)"
        per_file = max(600, int(max_chars) // len(rows))
        blocks = []
        used = 0
        for row in rows:
            remaining = int(max_chars) - used
            if remaining <= 100:
                break
            name = crypto.dec(row["filename"]) or "document"
            text = crypto.dec(row["content_text"]) or ""
            excerpt = select_excerpt(text, query, min(per_file, remaining - 60))
            block = f"[Attached document: {name}]\n{excerpt}"
            blocks.append(block)
            used += len(block) + 2
        return "\n\n".join(blocks) or "  (none attached)"
