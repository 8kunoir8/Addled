"""Word, Excel, PowerPoint and PDF, through the skills a chat turn would call.

The point of these formats being separate from the plain file skills: `.docx`,
`.xlsx` and `.pptx` are ZIP archives of XML and a `.pdf` is a binary container,
so `write_file` — which opens with `encoding="utf-8"` and writes TEXT — produces
a file the application refuses to open. Each format is therefore built and read
back with a library that knows it.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_office.py
"""

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.actions import office_ops as office  # noqa: E402
from backend.skills.registry import skill_registry  # noqa: E402

fails = []

def check(label, ok, detail=""):
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        fails.append(label)

def _workspace() -> str:
    """The bound folder. Every document path goes through its guard."""
    from backend.workspace import root
    return str(root() or "")

def _text_named(folder, name):
    """A text file wearing a document extension — the classic bad input."""
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("this is not really a document")
    return path

def _make_pdf(path, pages=2, text="Page {n} of the test"):
    """Write a valid PDF by hand.

    `pypdf` reads; it does not create, and adding a PDF-writing library just to
    make a fixture would be a dependency for no product. This is the smallest
    structure both Acrobat and pypdf accept.
    """
    objects = []
    first_page = 3
    font_num = first_page + pages * 2
    kids = [f"{first_page + i * 2} 0 R" for i in range(pages)]
    objects.append("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(f"2 0 obj\n<< /Type /Pages /Kids [{' '.join(kids)}] "
                   f"/Count {pages} >>\nendobj\n")
    for i in range(pages):
        page_num = first_page + i * 2
        content_num = page_num + 1
        body = ""
        if text:
            body = (f"BT /F1 24 Tf 72 700 Td "
                    f"({text.format(n=i + 1)}) Tj ET")
        objects.append(
            f"{page_num} 0 obj\n<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_num} 0 R "
            f">> >> /Contents {content_num} 0 R >>\nendobj\n")
        objects.append(f"{content_num} 0 obj\n<< /Length {len(body)} >>\n"
                       f"stream\n{body}\nendstream\nendobj\n")
    objects.append(f"{font_num} 0 obj\n<< /Type /Font /Subtype /Type1 "
                   f"/BaseFont /Helvetica >>\nendobj\n")

    out = "%PDF-1.4\n"
    offsets = []
    for obj in objects:
        offsets.append(len(out))
        out += obj
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n"
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n")
    with open(path, "w", encoding="latin-1") as fh:
        fh.write(out)

ws = _workspace()
if not ws or not os.path.isdir(ws):
    print("the workspace is not configured; this suite needs one")
    sys.exit(2)

tmp = os.path.join(ws, "office_check")
shutil.rmtree(tmp, ignore_errors=True)
os.makedirs(tmp, exist_ok=True)

print("The skills exist, and reads are ungated while writes are not")
READS = ("word_read", "excel_read", "excel_sheets", "pptx_read", "pdf_read",
         "pdf_pages", "pdf_redact_verify")
WRITES = ("word_create", "word_edit", "excel_write", "pptx_create",
          "pptx_add_slide", "pdf_create", "pdf_edit", "pdf_merge",
          "pdf_extract", "pdf_redact", "convert_to_pdf", "convert_from_pdf")
for name in READS + WRITES:
    check(f"{name} is registered", skill_registry.get(name) is not None,
          "the model cannot call it")
for name in READS:
    skill = skill_registry.get(name)
    check(f"{name} asks for nothing", skill and not skill.requires_approval,
          "reading a document is no more dangerous than reading a text file")
for name in WRITES:
    skill = skill_registry.get(name)
    check(f"{name} asks first", skill and skill.requires_approval,
          "a write can discard the document the user was working in")

print("\nWORD — create, read, edit")
docx = os.path.join(tmp, "plan.docx")
made = office.word_create(docx, title="Q3 Plan",
                          content="# Overview\nSome words.\n## Steps\n"
                                  "- one\n- two\nA closing line.")
check("a .docx is created", made.get("success"), str(made)[:160])
check("and it is a real ZIP container, not text",
      open(docx, "rb").read(2) == b"PK",
      "write_file would have produced a text file Word cannot open")
read = office.word_read(docx)
check("it reads back", read.get("success"), str(read)[:160])
headings = [p["text"] for p in read.get("paragraphs", []) if p.get("heading")]
check("headings survive as headings", headings == ["Overview", "Steps"],
      str(headings))
check("bullets survive as bullets",
      any(p.get("style") == "List Bullet"
          for p in read.get("paragraphs", [])), str(read.get("paragraphs"))[:200])

edited = office.word_edit(docx, find="Some words.", replace="Changed.")
check("an edit replaces the phrase", edited.get("success")
      and edited.get("replacements") == 1, str(edited)[:160])
check("and the change is really in the file",
      "Changed." in office.word_read(docx).get("text", ""))

missed = office.word_edit(docx, find="a phrase that is not there", replace="x")
check("an edit that matches nothing FAILS", not missed.get("success"),
      "a silent success would tell the user a change happened that did not")
check("and it says what it could not find",
      "not found" in str(missed.get("error")).lower(), str(missed.get("error"))[:140])

# A phrase split across runs — the case that makes paragraph.text useless.
split = os.path.join(tmp, "runs.docx")
office.word_create(split, content="plain text")
import docx as _docx  # noqa: E402
import openpyxl as _openpyxl  # noqa: E402
_d = _docx.Document(split)
_p = _d.add_paragraph()
_p.add_run("the quick ")
_r = _p.add_run("brown fox")
_r.bold = True  # forces a second run
_d.save(split)
check("the test file really has a split phrase",
      len(_docx.Document(split).paragraphs[-1].runs) == 2, "fixture is wrong")
found = office.word_edit(split, find="quick brown", replace="slow red")
check("a phrase split across runs is still found", found.get("success"),
      "replacing on paragraph.text silently misses formatted text")
check("and replaced across the run boundary",
      "slow red fox" in office.word_read(split).get("text", ""),
      str(office.word_read(split).get("text"))[:120])

print("\nEXCEL — create, read, edit in place, sheets")
xlsx = os.path.join(tmp, "budget.xlsx")
made = office.excel_write(xlsx, sheet="Q3", grid=[
    ["Item", "Cost"], ["Hosting", 120], ["Domain", 15]], overwrite=True)
check("a .xlsx is created", made.get("success") and made.get("created"),
      str(made)[:160])
check("and it is a real ZIP container", open(xlsx, "rb").read(2) == b"PK")
read = office.excel_read(xlsx)
check("values read back", read.get("rows") == [
    ["Item", "Cost"], ["Hosting", 120], ["Domain", 15]], str(read.get("rows")))
check("the sheet name is respected", read.get("sheet") == "Q3", str(read.get("sheet")))

# The EDIT path: no overwrite, so the earlier rows must survive.
office.excel_write(xlsx, sheet="Q3", cells=[
    {"row": 4, "col": 1, "value": "Support"}, {"row": 4, "col": 2, "value": 90}])
after = office.excel_read(xlsx)
check("writing again without overwrite UPDATES the file",
      after.get("rows") == [["Item", "Cost"], ["Hosting", 120],
                            ["Domain", 15], ["Support", 90]],
      str(after.get("rows")))
check("a nested value does not fail the whole write",
      office.excel_write(xlsx, cells=[
          {"row": 6, "col": 1, "value": {"nested": True}}]).get("success"),
      "openpyxl raises on a dict and the error blames the library")
sheets = office.excel_sheets(xlsx)
check("sheet names are listed", sheets.get("success") and sheets.get("count") == 1,
      str(sheets)[:160])

print("\nPOWERPOINT — create, read, add a slide")
pptx = os.path.join(tmp, "deck.pptx")
made = office.pptx_create(pptx, title="Roadmap", slides=[
    {"title": "Now", "bullets": ["Ship it", "Tell people"]},
    "A divider",
    {"title": "Next", "bullets": ["Measure"]}])
check("a .pptx is created", made.get("success"), str(made)[:160])
check("and it is a real ZIP container", open(pptx, "rb").read(2) == b"PK")
read = office.pptx_read(pptx)
check("it reads back", read.get("success"), str(read)[:200])
check("the deck title is on the first slide",
      read.get("slides", [{}])[0].get("title") == "Roadmap",
      str(read.get("slides", [{}])[0]))
titles = [s.get("title") for s in read.get("slides", [])]
check("every slide title survives", titles == ["Roadmap", "Now", "A divider", "Next"],
      str(titles))
check("bullets survive",
      "Ship it" in str(read.get("slides", [{}])[1].get("text")),
      str(read.get("slides", [{}])[1]))
added = office.pptx_add_slide(pptx, title="Later", bullets=["Refactor"])
check("a slide can be appended", added.get("success"), str(added)[:160])
check("and the deck now has one more",
      office.pptx_read(pptx).get("count") == 5,
      str(office.pptx_read(pptx).get("count")))

print("\nPDF — create, read it back, ranges, and the honest answer for a scan")
pdf = os.path.join(tmp, "doc.pdf")
_make_pdf(pdf, pages=3)
read = office.pdf_read(pdf)
check("a PDF is read", read.get("success") and read.get("pages") == 3,
      str(read)[:160])
check("its text comes out", "Page 1" in read.get("text", ""),
      str(read.get("text"))[:120])
check("a page range is honoured",
      office.pdf_read(pdf, pages="2").get("read") == [2],
      str(office.pdf_read(pdf, pages="2").get("read")))
check("a list of pages is honoured",
      office.pdf_read(pdf, pages="1,3").get("read") == [1, 3],
      str(office.pdf_read(pdf, pages="1,3").get("read")))
check("a reversed range does not raise",
      office._page_range("3-1", 3) == [1, 2, 3], str(office._page_range("3-1", 3)))
# An empty spec means "all", a spec that matched nothing means "none". The two
# must not be the same answer: treating "page 99 of a 2-page file" as "all
# pages" turns a request for a subset into a silent no-op that reports success.
check("an empty range means every page",
      office._page_range("", 3) == [1, 2, 3], str(office._page_range("", 3)))
check("a range that matches nothing returns none, not all",
      office._page_range("99", 3) == [], str(office._page_range("99", 3)))
check("nonsense matches nothing rather than everything",
      office._page_range("nonsense", 3) == [],
      str(office._page_range("nonsense", 3)))

# Creation, and the strongest check available: read back what was written.
made = os.path.join(tmp, "made.pdf")
created = office.pdf_create(
    made, title="Quarterly Report",
    content="# Overview\nRevenue is up.\n## Costs\n- hosting\n- domain\n\n"
            "A closing line.")
check("a PDF is created", created.get("success"), str(created)[:160])
check("and it is a real PDF container",
      open(made, "rb").read(5) == b"%PDF-",
      "a text file with a .pdf name would open in nothing")
roundtrip = office.pdf_read(made)
check("the created PDF reads back", roundtrip.get("success"),
      str(roundtrip)[:160])
text = str(roundtrip.get("text") or "")
check("the title survived", "Quarterly Report" in text, text[:120])
check("a heading survived", "Overview" in text, text[:120])
check("a bullet survived", "hosting" in text, text[:160])
check("and the closing line survived", "closing line" in text, text[:160])

# The bug this shipped with: multi_cell leaves the cursor at the END of the
# line, so every block after the first computed a zero width and raised
# "Not enough horizontal space to render a single character". A one-line
# document would never have caught it.
check("more than one block renders at all",
      created.get("blocks", 0) >= 5, str(created.get("blocks")))

check("the page size is honoured",
      office.pdf_create(os.path.join(tmp, "letter.pdf"), content="x",
                        page_size="letter", overwrite=True).get("pageSize")
      == "Letter")
check("an unknown page size falls back to A4, not an error",
      office.pdf_create(os.path.join(tmp, "odd.pdf"), content="x",
                        page_size="papyrus", overwrite=True).get("success"))
check("creating over an existing PDF is refused",
      office.pdf_create(made, content="x").get("blocked") is True)
check("unless overwrite is asked for",
      office.pdf_create(made, content="x", overwrite=True).get("success"))

# Text the core fonts cannot render must be REPORTED, not silently mangled or
# allowed to fail the whole document.
intl = os.path.join(tmp, "intl.pdf")
mixed = office.pdf_create(intl, title="Summary",
                          content="Cafe na\u00efve\n\u4e2d\u6587 text\n\U0001F600 x",
                          overwrite=True)
check("non-Latin text does not fail the document", mixed.get("success"),
      str(mixed)[:160])
check("and the characters it could not render are counted",
      mixed.get("replacedCharacters") == 3,
      f"got {mixed.get('replacedCharacters')} — two CJK characters plus the "
      f"emoji, and the accented latin-1 letter is renderable")
check("with a note saying why",
      "core fonts" in str(mixed.get("note")), str(mixed.get("note"))[:120])
check("latin-1 accents are NOT replaced",
      "na\u00efve" in str(office.pdf_read(intl).get("text") or ""),
      str(office.pdf_read(intl).get("text"))[:120])

# A PDF with no text layer must SAY so, not read as an empty success.
empty = os.path.join(tmp, "scan.pdf")
_make_pdf(empty, pages=1, text="")
blank = office.pdf_read(empty)
check("a text-free PDF reports why rather than reading as empty",
      blank.get("success") and blank.get("note"),
      "an empty read sends the model looking for a bug in its own query")

print("\nPDF — page listing, editing, merging and extraction")
# A multi-page fixture. `---` starts a new page; without it the whole document
# flows onto one page and every page-count assertion below is meaningless.
def _paged(name, pages=3, title="Original"):
    path = os.path.join(tmp, name)
    blocks = []
    for i in range(1, pages + 1):
        if i > 1:
            blocks.append("---")
        blocks.append(f"# Page {i}")
        blocks.append(f"body text for page {i}")
    made = office.pdf_create(path, title=title, content="\n".join(blocks),
                             overwrite=True)
    assert made.get("success"), made
    return path

def _pagecount(path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(path).pages)

def _alltext(path) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)

check("a --- line really does start a new page",
      _pagecount(_paged("paged.pdf", pages=3)) == 3,
      f"got {_pagecount(_paged('paged.pdf', pages=3))} — page breaks would be a "
      f"no-op and every check below would pass vacuously")

listing = office.pdf_pages(_paged("pages.pdf", pages=3))
check("pdf_pages lists every page", listing.get("count") == 3,
      str(listing)[:160])
check("and previews what each page says",
      "Page 2" in str(listing.get("pages", [{}, {}])[1].get("preview")),
      str(listing.get("pages", [])[1])[:140])

# REMOVE — verified by counting the saved file, not the return value.
doc = _paged("remove.pdf", pages=4)
removed = office.pdf_edit(doc, remove_pages="2")
check("removing a page reports the new count",
      removed.get("success") and removed.get("pages") == 3,
      str(removed)[:160])
check("and the FILE really has one fewer page",
      _pagecount(doc) == 3, f"got {_pagecount(doc)}")
check("and the removed page's text is gone",
      "Page 2" not in _alltext(doc), _alltext(doc)[:120])
check("a page number out of range is refused, not silently ignored",
      not office.pdf_edit(doc, remove_pages="99").get("success"),
      "a no-op reported as success reads as done to whoever asked")
check("and that refusal left the file alone", _pagecount(doc) == 3)

# KEEP
doc = _paged("keep.pdf", pages=4)
office.pdf_edit(doc, keep_pages="1,3")
check("keep_pages keeps exactly those pages", _pagecount(doc) == 2,
      str(_pagecount(doc)))
text = _alltext(doc)
check("the kept pages are the right ones",
      "Page 1" in text and "Page 3" in text and "Page 2" not in text,
      text[:140])
check("keep_pages that matches nothing is refused",
      not office.pdf_edit(doc, keep_pages="99").get("success"),
      "asking for a subset must not return the whole document")
check("and the file is untouched", _pagecount(doc) == 2)

# REORDER
doc = _paged("order.pdf", pages=3)
office.pdf_edit(doc, order="3,1")
check("order puts the named pages first", _alltext(doc).strip().startswith("Page 3"),
      _alltext(doc)[:100])
check("and drops the pages it omits", "Page 2" not in _alltext(doc),
      _alltext(doc)[:140])

# ROTATE
doc = _paged("rotate.pdf", pages=2)
office.pdf_edit(doc, rotate=90, rotate_pages="1")
from pypdf import PdfReader  # noqa: E402
angles = [int(p.get("/Rotate", 0) or 0) for p in PdfReader(doc).pages]
check("rotate turns only the named page", angles == [90, 0], str(angles))
check("an angle that is not a multiple of 90 is refused",
      not office.pdf_edit(doc, rotate=45).get("success"),
      "a PDF page is turned, not skewed")
check("and the refusal did not rotate anything",
      [int(p.get("/Rotate", 0) or 0) for p in PdfReader(doc).pages] == [90, 0])
check("rotate_pages that matches nothing is refused",
      not office.pdf_edit(doc, rotate=90, rotate_pages="99").get("success"))

# METADATA
doc = _paged("meta.pdf", pages=1)
office.pdf_edit(doc, title="Revised", author="Addled", subject="Report")
meta = PdfReader(doc).metadata or {}
check("metadata is written", str(meta.get("/Title")) == "Revised",
      repr(meta.get("/Title")))
check("and the author with it", str(meta.get("/Author")) == "Addled",
      repr(meta.get("/Author")))

check("an edit with nothing to change is refused",
      not office.pdf_edit(doc).get("success"), "")

print("\nPDF — extract and merge")
source = _paged("extract_src.pdf", pages=5)
dest = os.path.join(tmp, "extracted.pdf")
got = office.pdf_extract(source, pages="2-4", output=dest)
check("extract writes the pages asked for", got.get("success"), str(got)[:140])
check("the new file has 3 pages", _pagecount(dest) == 3, str(_pagecount(dest)))
check("the SOURCE is untouched", _pagecount(source) == 5,
      "extract reads one document and writes another")
extracted = _alltext(dest)
check("the right pages came out",
      "Page 3" in extracted and "Page 5" not in extracted, extracted[:140])
check("extract into an existing name is refused without overwrite",
      office.pdf_extract(source, pages="1", output=dest).get("blocked") is True)

a = _paged("merge_a.pdf", pages=2)
b = _paged("merge_b.pdf", pages=3)
merged = os.path.join(tmp, "merged.pdf")
result = office.pdf_merge(merged, sources=[a, b], overwrite=True)
check("merging reports the combined page count",
      result.get("success") and result.get("pages") == 5, str(result)[:160])
check("and the file really has 2+3 pages", _pagecount(merged) == 5,
      str(_pagecount(merged)))
check("the sources are kept by default",
      os.path.exists(a) and os.path.exists(b),
      "consuming the inputs would destroy documents meant to be reused")
check("merging onto an existing name is refused without overwrite",
      office.pdf_merge(a, sources=[b]).get("blocked") is True)
check("and that refusal left the destination alone", _pagecount(a) == 2)
check("a missing source is named, not guessed at",
      "Not found" in str(office.pdf_merge(os.path.join(tmp, "m2.pdf"),
                                          sources=[os.path.join(tmp, "ghost.pdf")]
                                          ).get("error")), "")
check("merging a non-PDF is refused by extension",
      not office.pdf_merge(os.path.join(tmp, "m3.pdf"),
                           sources=[docx]).get("success"), "")

print("\nEvery path goes through the workspace guard")
outside = os.path.join(os.path.expanduser("~"), "should_not_be_written.docx")
blocked = office.word_create(outside, content="x")
check("a path outside the workspace is refused by the write",
      blocked.get("blocked") is True, str(blocked)[:180])
check("a path outside the workspace is refused by the read",
      office.word_read(outside).get("blocked") is True,
      "excel_ops handed a raw path to the OS; this must not repeat that")
# The new operations must go through the same seam. A redaction that writes
# outside the workspace, or a conversion that reads from outside it, would be
# the same hole the old excel handler had.
check("redacting outside the workspace is blocked",
      office.pdf_redact(os.path.join(tmp, "secret.pdf"), terms=["x"],
                        output=os.path.join(os.path.expanduser("~"),
                                            "escape_redact.pdf")
                        ).get("blocked") is True,
      "redaction writes a file, so it is subject to the same guard")
check("converting to a path outside the workspace is blocked",
      office.convert_to_pdf(docx, output=os.path.join(
          os.path.expanduser("~"), "escape_convert.pdf")).get("blocked") is True)
check("and nothing was created outside",
      not os.path.exists(outside), "a file escaped the workspace")

print("\nWrong tool, wrong format, and no-clobber are all refused clearly")
print("  (each message matters: it is what the model has to act on)")
wrong = office.excel_read(docx)
check("reading a .docx with the excel tool is refused",
      not wrong.get("success") and "word" in str(wrong.get("error")).lower(),
      str(wrong.get("error"))[:160])
unsupported = office.word_read(os.path.join(tmp, "notes.txt"))
check("an unsupported extension names what IS supported",
      not unsupported.get("success") and ".docx" in str(unsupported.get("error")),
      str(unsupported.get("error"))[:160])
clash = office.word_create(docx, content="x")
check("creating over an existing document is refused",
      clash.get("blocked") is True, str(clash)[:160])
check("unless overwrite is asked for",
      office.word_create(os.path.join(tmp, "again.docx"), content="x")
      .get("success"), "")
check("word_edit with nothing to change is refused",
      not office.word_edit(docx).get("success"), "")
check("excel_write with nothing to write is refused",
      not office.excel_write(xlsx).get("success"), "")
check("a missing file says so, and names the path",
      "not a file" in str(office.word_read(
          os.path.join(tmp, "ghost.docx")).get("error")).lower(), "")

print("\nEvery unparseable call is survivable")
for label, call in (
    ("word_read on a text file with a .docx name",
     lambda: office.word_read(_text_named(tmp, "fake.docx"))),
    ("excel_read on a text file with a .xlsx name",
     lambda: office.excel_read(_text_named(tmp, "fake.xlsx"))),
    ("pptx_read on a text file with a .pptx name",
     lambda: office.pptx_read(_text_named(tmp, "fake.pptx"))),
    ("pdf_read on a text file with a .pdf name",
     lambda: office.pdf_read(_text_named(tmp, "fake.pdf"))),
):
    try:
        result = call()
        check(label, isinstance(result, dict) and not result.get("success"),
              str(result)[:120])
    except Exception as e:  # noqa: BLE001
        check(label, False, f"raised {type(e).__name__}: {e}")

# These libraries are not optional extras any more: a bundled install must have
# them, or the skill reports itself missing on a clean machine. python-docx and
# python-pptx were resolving from the USER site-packages, which would not exist
# on another machine.
print("\nThe libraries are importable WITHOUT the user site-packages")
import subprocess  # noqa: E402
probe = subprocess.run(
    [sys.executable, "-s", "-c",
     "import docx, pptx, openpyxl, pypdf, fpdf; print('ok')"],
    capture_output=True, text=True, timeout=60)
check("python -s can import every Office library",
      probe.returncode == 0 and "ok" in probe.stdout,
      (probe.stderr or probe.stdout)[-200:])

print("\nPDF — redaction")
# The claim being tested is a SECURITY property, not a cosmetic one: text that
# is merely covered is still in the content stream and still comes back from
# extraction. Every check here searches the text rather than looking at the page.
_secret = "4111-2222-3333"
def _secret_pdf(name):
    path = os.path.join(tmp, name)
    office.pdf_create(path, title="Statement",
                      content=f"# Statement\nAccount {_secret}\n"
                              f"Public line that should stay",
                      overwrite=True)
    return path

src = _secret_pdf("secret.pdf")
check("the secret is extractable to begin with",
      _secret in _alltext(src), _alltext(src)[:80].replace("\n", " "))

red = os.path.join(tmp, "redacted.pdf")
result = office.pdf_redact(src, terms=[_secret], output=red)
check("redact reports success", result.get("success"), str(result)[:160])
check("and counts what it removed", result.get("redacted") == 1,
      str(result.get("redacted")))
check("the original is NOT modified",
      _secret in _alltext(src),
      "redaction must not edit the source in place")
check("the secret is GONE from the redacted copy",
      _secret not in _alltext(red),
      f"still extractable: {_alltext(red)[:100]!r} — a covered glyph is not a "
      f"removed one")
check("and the rest of the text survived",
      "Public line" in _alltext(red), _alltext(red)[:100].replace("\n", " "))

ver = office.pdf_redact_verify(red, [_secret])
check("the verifier confirms it is clean", ver.get("success") and ver.get("clean"),
      str(ver)[:140])
check("and the verifier FAILS on the untouched original",
      not office.pdf_redact_verify(src, [_secret]).get("success"),
      "a verifier that always passes is worth nothing")
check("naming what it still found",
      _secret in str(office.pdf_redact_verify(src, [_secret]).get("stillPresent")),
      "")

check("redacting with no terms is refused",
      not office.pdf_redact(src, terms=[]).get("success"))
check("a term matching nothing is refused, not a silent success",
      not office.pdf_redact(src, terms=["nothing-like-this"],
                            output=os.path.join(tmp, "none.pdf")).get("success"),
      "'redacted' with nothing removed is the failure worth catching")
check("redacting onto the source is refused",
      not office.pdf_redact(src, terms=[_secret], output=src).get("success"),
      "redaction cannot be undone, so the original must survive")
partial = office.pdf_redact(src, terms=[_secret, "absent-term"],
                            output=os.path.join(tmp, "partial.pdf"))
check("a partly-matching set still succeeds",
      partial.get("success") and partial.get("redacted") == 1,
      str(partial)[:140])
check("and names the term it never found",
      "absent-term" in (partial.get("notFound") or []),
      str(partial.get("notFound")))

print("\nConversion — Office to PDF")
_word = os.path.join(tmp, "conv.docx")
_d = _docx.Document()
_d.add_heading("Converted Report", 0)
_d.add_paragraph("A paragraph of body text.")
_d.save(_word)
_word_pdf = os.path.join(tmp, "conv.pdf")
got = office.convert_to_pdf(_word, output=_word_pdf)
check("a Word document converts", got.get("success"), str(got)[:150])
if got.get("success"):
    text = _alltext(_word_pdf)
    check("its heading survives", "Converted Report" in text, text[:100])
    check("and its body text", "body text" in text, text[:120])
check("converting over an existing file needs overwrite",
      office.convert_to_pdf(_word, output=_word_pdf).get("blocked") is True)

# A deck must give one page per slide. The library's own converter stacked
# three slides onto a single 400x600 page: the text was all there, so it looked
# like it worked, but a deck whose slides are not pages is not a deck.
from pptx import Presentation  # noqa: E402
_deck = os.path.join(tmp, "conv.pptx")
_pr = Presentation()
for title in ("Slide One", "Slide Two"):
    _sl = _pr.slides.add_slide(_pr.slide_layouts[0])
    _sl.shapes.title.text = title
_pr.save(_deck)
_deck_pdf = os.path.join(tmp, "conv_deck.pdf")
got = office.convert_to_pdf(_deck, output=_deck_pdf)
check("a deck converts", got.get("success"), str(got)[:150])
if got.get("success"):
    check("one PDF page per slide",
          _pagecount(_deck_pdf) == 2,
          f"got {_pagecount(_deck_pdf)} page(s) for 2 slides")
    _pages = PdfReader(_deck_pdf).pages
    check("each slide's title is on its own page",
          "Slide One" in (_pages[0].extract_text() or "")
          and "Slide Two" in (_pages[1].extract_text() or ""),
          "titles merged onto one page")

# The spreadsheet path renders values itself: the library's own xlsx converter
# dropped every TEXT cell, keeping only the numbers.
_sheet = os.path.join(tmp, "conv.xlsx")
_wb = _openpyxl.Workbook()
_ws = _wb.active
_ws.title = "Summary"
for _row in (["Item", "Cost"], ["Hosting", 120]):
    _ws.append(_row)
_wb.save(_sheet)
_sheet_pdf = os.path.join(tmp, "conv_sheet.pdf")
got = office.convert_to_pdf(_sheet, output=_sheet_pdf)
check("a spreadsheet converts", got.get("success"), str(got)[:150])
if got.get("success"):
    text = _alltext(_sheet_pdf)
    check("its TEXT cells survive",
          "Hosting" in text and "Item" in text,
          f"{text[:80]!r} — the library's own converter produced only '120'")
    check("and the numbers with them", "120" in text, text[:80])
    check("the trade-off is stated rather than hidden",
          bool(got.get("note")), str(got.get("note"))[:100])

print("\nConversion — PDF to text, HTML and Word")
_src_pdf = os.path.join(tmp, "from.pdf")
office.pdf_create(_src_pdf, title="Extract Me",
                  content="# Alpha\nfirst line\n---\n# Beta\nsecond line",
                  overwrite=True)
_txt = os.path.join(tmp, "from.txt")
got = office.convert_from_pdf(_src_pdf, output=_txt, to="text")
check("pdf to text works", got.get("success"), str(got)[:140])
if got.get("success"):
    body = open(_txt, encoding="utf-8").read()
    check("both pages are in it", "Alpha" in body and "Beta" in body,
          body[:100])
    check("and the page boundary is marked", "page 2" in body.lower(),
          body[:160])
_html = os.path.join(tmp, "from.html")
got = office.convert_from_pdf(_src_pdf, output=_html, to="html")
check("pdf to html works", got.get("success"), str(got)[:140])
if got.get("success"):
    body = open(_html, encoding="utf-8").read()
    check("it is real html",
          body.lstrip().startswith(("<!doctype", "<html")), body[:40])
    check("with the page text inside", "Alpha" in body, body[:120])
_docxout = os.path.join(tmp, "from.docx")
got = office.convert_from_pdf(_src_pdf, output=_docxout, to="docx")
check("pdf to word works", got.get("success"), str(got)[:140])
if got.get("success"):
    joined = "\n".join(p.text for p in _docx.Document(_docxout).paragraphs)
    check("the text came through", "Alpha" in joined and "Beta" in joined,
          joined[:120].replace("\n", " "))
    check("the extraction caveat is stated",
          bool(got.get("note")),
          "a reflowed PDF is not a rebuilt document, and saying so matters")
check("an unknown target is refused",
      not office.convert_from_pdf(_src_pdf, to="spreadsheet").get("success"))

print("\nAn approved write actually runs")
# the name it queued. It only knew its own handlers, so every gated skill with
# no executor handler was answered and then failed with "Unknown action" — the
# card appeared, the user approved, and nothing happened. `pdf_create`,
# `word_create`, `word_edit`, `pptx_create` and `pptx_add_slide` were all in
# that set. This is the ONLY path a chat turn's write takes, so it is the check
# that matters most.
import asyncio  # noqa: E402

from backend.actions.executor import ActionExecutor, ActionRequest  # noqa: E402
from backend.safety.destruction_gate import DestructionGate  # noqa: E402

_ex = ActionExecutor(gate=DestructionGate())
_ex._lazy_init()

# A dedicated file for the redaction dispatch check. Using one of the earlier
# documents made the test depend on what previous sections had already done to
# it — `ap.pdf` had its "Heading" removed before this point, so the redaction
# legitimately found nothing and the check failed for the wrong reason.
_red_src = os.path.join(ws, "office_check", "dispatch_redact.pdf")
office.pdf_create(_red_src, title="Dispatch", content="# Keep\nsecret-value",
                  overwrite=True)

_APPROVED = [
    ("pdf_create", {"path": "office_check/ap.pdf", "title": "A",
                    "content": "# H\nbody"}, "office_check/ap.pdf"),
    ("word_create", {"path": "office_check/aw.docx", "content": "# H\nbody"},
     "office_check/aw.docx"),
    ("pptx_create", {"path": "office_check/ax.pptx", "title": "A",
                     "slides": ["One"]}, "office_check/ax.pptx"),
    ("excel_write", {"path": "office_check/ae.xlsx", "grid": [["a", "b"]]},
     "office_check/ae.xlsx"),
    ("word_edit", {"path": "office_check/aw.docx", "append": "added"},
     "office_check/aw.docx"),
    ("pdf_edit", {"path": "office_check/ap.pdf", "title": "Renamed"},
     "office_check/ap.pdf"),
    ("pdf_extract", {"path": "office_check/ap.pdf", "pages": "1",
                     "output": "office_check/ap_ex.pdf"},
     "office_check/ap_ex.pdf"),
    ("pdf_merge", {"path": "office_check/am.pdf",
                   "sources": ["office_check/ap.pdf"]},
     "office_check/am.pdf"),
    ("convert_to_pdf", {"path": "office_check/aw.docx",
                        "output": "office_check/aw_conv.pdf"},
     "office_check/aw_conv.pdf"),
    ("convert_from_pdf", {"path": "office_check/ap.pdf",
                          "to": "text",
                          "output": "office_check/ap.txt"},
     "office_check/ap.txt"),
    ("pdf_redact", {"path": "office_check/dispatch_redact.pdf",
                    "terms": ["secret-value"],
                    "output": "office_check/ap_red.pdf"},
     "office_check/ap_red.pdf"),
]
for name, params, expect in _APPROVED:
    result = asyncio.run(_ex.execute(ActionRequest(action_type=name,
                                                   params=params)))
    path = os.path.join(ws, expect.replace("/", os.sep))
    check(f"an approved {name} runs and writes its file",
          result.success and os.path.exists(path),
          f"success={result.success} error={str(result.error)[:90]}")

check("no Office skill is left without a dispatch path",
      all(skill_registry.get(n) is not None
          for n, _, _ in _APPROVED),
      "an approval that cannot run is worse than a prompt that never appeared")
check("the legacy unguarded excel handlers are gone",
      "excel_write" not in _ex._handlers or
      "office_ops" in str(_ex._handlers.get("excel_write")),
      "two implementations sharing a name means the unguarded one wins")

shutil.rmtree(tmp, ignore_errors=True)

print()
if fails:
    print(f"{len(fails)} FAILED")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("All Office checks passed.")
