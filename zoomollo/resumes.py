"""Read résumé files on this computer: PDF, Word (.docx), plain text / .rtf.

Also pulls the basics out of an uploaded résumé (name, email, phone,
city/state) and estimates years of experience from the dates in it.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from xml.etree import ElementTree

from .discover import STATES


def extract_text(data: bytes, filename: str = "", content_type: str = "") -> str:
    """Plain text of a résumé file. Raises ValueError for formats we can't read."""
    name = filename.lower()
    ctype = (content_type or "").lower()
    if name.endswith(".pdf") or "pdf" in ctype or data[:5] == b"%PDF-":
        return _pdf(data)
    if name.endswith(".docx") or "wordprocessingml" in ctype or (data[:2] == b"PK" and b"word/" in data[:4000]):
        return _docx(data)
    if name.endswith(".doc") or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError("old Word .doc files can't be read; save it as .docx or PDF")
    if name.endswith(".rtf") or data[:5] == b"{\\rtf":
        return _rtf(data.decode("utf-8", "replace"))
    return data.decode("utf-8", "replace")


def _pdf(data: bytes) -> str:
    import logging

    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed-PDF chatter
    reader = PdfReader(io.BytesIO(data))
    return _tidy("\n".join((page.extract_text() or "") for page in reader.pages[:15]))


def _docx(data: bytes) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    lines = []
    for para in root.iter(ns + "p"):
        text = "".join((t.text or "") if t.tag == ns + "t" else ("\t" if t.tag == ns + "tab" else "")
                       for t in para.iter() if t.tag in (ns + "t", ns + "tab"))
        lines.append(text)
    return _tidy("\n".join(lines))


def _rtf(text: str) -> str:
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\{\\\*[^{}]*\}|\\[a-z]+-?\d* ?|[{}]", "", text)
    return _tidy(text)


def _tidy(text: str) -> str:
    text = text.replace(" ", " ").replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# --- basics from an uploaded résumé -------------------------------------------------

_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_CITY_ST = re.compile(r"\b([A-Z][a-zA-Z .'-]{1,30}),\s*(" + "|".join(STATES) + r")\b(?:\s+\d{5})?")
_NAME = re.compile(r"^[A-Z][a-zA-Z'’-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z'’-]+){1,2}$")


def basics(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines()[:8]:
        line = line.strip()
        if _NAME.match(line) and len(line) < 40 and not line.isupper():
            out["name"] = line
            break
        if line.isupper() and 4 < len(line) < 40 and _NAME.match(line.title()):
            out["name"] = line.title()
            break
    m = _EMAIL.search(text)
    if m:
        out["email"] = m.group(0).lower()
    from .extract import find_phones
    phones = find_phones(text[:3000])
    if phones:
        out["phone"] = sorted(phones)[0]
    m = _CITY_ST.search(text[:3000])
    if m:
        out["city"], out["state"] = m.group(1).strip(), m.group(2)
    return out


def years_experience(text: str, today: date | None = None) -> int | None:
    """Rough years of experience from the earliest work year mentioned (education years
    near words like 'B.S.', 'University' are ignored)."""
    today = today or date.today()
    years = []
    for m in re.finditer(r"\b(19[6-9]\d|20[0-4]\d)\b", text):
        around = text[max(0, m.start() - 80): m.end() + 3].lower()
        if re.search(r"universit|college|bachelor|master|\b(?:b\.?s|b\.?a|m\.?b\.?a|m\.?s)\b\.?|degree|graduat|\bgpa\b|"
                     r"high school|cpa exam", around):
            continue
        y = int(m.group(1))
        if y <= today.year:
            years.append(y)
    if not years:
        return None
    n = today.year - min(years)
    return n if 1 <= n <= 50 else None
