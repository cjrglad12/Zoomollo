import io
import zipfile

from zoomollo import lens
from zoomollo.resumes import basics, extract_text, years_experience

RESUME = """JANE SMITH
Milwaukee, WI 53202 | jane.smith@example.org | (414) 555-0199
Senior Tax Manager, Baker Tilly (2016 - present)
- Reviewed corporate and partnership returns; review of tax provisions
Manager, Tax at EY (2012-2016). Prepared 1120 returns.
B.S. Accounting, University of Wisconsin, 2008"""


def test_parse_plain_english_example():
    gs = lens.parse("tax manager with experience reviewing who lives in Texas or Wisconsin")
    assert [(g.label, g.kind) for g in gs] == [("tax manager", "keyword"), ("reviewing", "keyword"),
                                              ("Texas | Wisconsin", "location")]
    assert [g.color for g in gs] == [0, 1, 2]
    assert lens.describe(gs) == "tax manager, reviewing, Texas | Wisconsin"


def test_parse_exact_and_alternatives():
    gs = lens.parse("tax manager, review | audit, TX | WI")
    assert [g.label for g in gs] == ["tax manager", "review | audit", "Texas | Wisconsin"]
    gs = lens.parse("HVAC technician or service tech with EPA certification in MO or IL")
    assert [g.label for g in gs] == ["HVAC technician | service tech", "EPA certification", "Missouri | Illinois"]
    # lower-case "in"/"or"/"me" aren't states
    assert all(g.kind == "keyword" for g in lens.parse("experience in payroll or me"))


def test_match_word_forms_reversed_titles_and_states():
    gs = lens.parse("tax manager with experience reviewing who lives in Texas or Wisconsin")
    m = lens.match(RESUME, gs)
    assert m.hits == [2, 2, 2]                    # "Tax Manager" + "Manager, Tax"; "Reviewed" + "review"; WI + Wisconsin
    assert lens.fit_label(m, gs) == "strong fit"
    assert lens.match("Texas Instruments", lens.parse("TX")).hits == [1]
    assert lens.match("tx 123", lens.parse("TX")).hits == [0]     # abbreviations only in capitals


def test_highlight_colours_and_escaping():
    gs = lens.parse("tax manager, review")
    out = lens.highlight("<b>Tax Manager</b> who reviews", gs)
    assert '<mark class="k0" id="m0">Tax Manager</mark>' in out and '<mark class="k1"' in out
    assert "&lt;b&gt;" in out and "<b>" not in out


def test_reading_files():
    assert extract_text(b"Hello resume", "cv.txt") == "Hello resume"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                   '<w:p><w:r><w:t>Jane Smith</w:t></w:r></w:p><w:p><w:r><w:t>Tax Manager</w:t></w:r></w:p>'
                   '</w:body></w:document>')
    assert extract_text(buf.getvalue(), "cv.docx") == "Jane Smith\nTax Manager"
    assert "Tax Manager" in extract_text(_tiny_pdf("Jane Smith Tax Manager"), "cv.pdf")
    try:
        extract_text(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "old.doc")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert ".docx" in str(e)


def test_basics_and_years():
    b = basics(RESUME)
    assert b == {"name": "Jane Smith", "email": "jane.smith@example.org", "phone": "+14145550199",
                 "city": "Milwaukee", "state": "WI"}
    assert years_experience(RESUME) >= 13                 # from 2012, not the 2008 degree


def _tiny_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out
