"""Office documents — read, create and edit Word, Excel, PowerPoint and PDF.

Why this exists rather than plain `write_file`: `.docx`, `.xlsx` and `.pptx` are
ZIP archives of XML and a `.pdf` is a binary container. `write_file` opens with
`encoding="utf-8"` and writes TEXT, so "writing" one of these produces a text
file with a document extension — Word, Excel and Acrobat all report the result
as corrupt. A document has to be built with a library that knows the format.

Two rules, both taken from the code this replaces:

  * **Every path goes through the workspace guard.** `excel_ops` took a raw path
    and handed it to the OS, so it could read and write anywhere on the machine
    regardless of the file-access mode. The guard is the one seam every file
    skill shares; a new reader that skips it is a hole.

  * **Nothing is silent about what it did not do.** A library that models
    paragraphs and cells does not model everything a document contains. Every
    write reports what it changed, because "it saved" and "it saved your
    formatting" are different claims and only one is usually true.

A PDF is readable, creatable and structurally editable — pages, order, rotation,
metadata, merging and extraction. What it is NOT is text-editable: a PDF stores
its content as positioned glyphs in a compressed stream, so there is no
paragraph to find and replace. `pdf_edit` says so rather than half-doing it, and
the honest boundary is worth more than a feature that silently reflows a page.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("addled.office")

MAX_PARAGRAPHS = 4000
MAX_SHEETS = 50
MAX_SLIDES = 300
MAX_PDF_PAGES = 500
MAX_CELL_VALUE = 8000

def _guard(path: str) -> tuple[str | None, dict | None]:
    """(usable path, refusal). One of the two is always None."""
    from backend.workspace import resolve
    resolved, reason = resolve(path)
    if reason:
        log.info("document access refused: %s", reason)
        return None, {"success": False, "error": reason, "blocked": True,
                      "path": str(path)}
    return str(resolved), None

def _missing(library: str, package: str) -> dict:
    return {"success": False,
            "error": (f"{library} is not installed, so this format cannot be "
                      f"handled. Run: pip install {package}")}

def _kind(path: str) -> str:
    """`word` / `excel` / `powerpoint` / `pdf` / `` for anything else."""
    ext = os.path.splitext(str(path or ""))[1].lower()
    return {
        ".docx": "word",
        ".xlsx": "excel", ".xlsm": "excel",
        ".pptx": "powerpoint",
        ".pdf": "pdf",
    }.get(ext, "")

def _require(path: str, wanted: tuple[str, ...]) -> dict | None:
    """Refuse early and clearly when the extension is not what this call is for.

    Naming the actual extension matters: "not a Word document" is much harder to
    act on than "that is a .xlsx — use the Excel tool".
    """
    kind = _kind(path)
    if not kind:
        ext = os.path.splitext(str(path or ""))[1] or "(no extension)"
        return {"success": False,
                "error": (f"'{ext}' is not an Office or PDF format. Supported: "
                          f".docx, .xlsx/.xlsm, .pptx, .pdf. Use the plain file "
                          f"skills for anything else.")}
    if kind not in wanted:
        return {"success": False,
                "error": (f"That is a {kind} file, not {wanted[0]}. Use the "
                          f"{wanted[0]} tool for it instead.")}
    return None

def _no_clobber(path: str, overwrite: bool) -> dict | None:
    """Refuse to replace an existing document unless asked.

    The same rule `FileOps.copy`/`move` already follow: a caller that derived a
    path from a guess must not destroy the file that was there.
    """
    if os.path.exists(path) and not overwrite:
        return {"success": False, "blocked": True, "path": path,
                "error": (f"{os.path.basename(path)} already exists. Pass "
                          f"overwrite=true to replace it, or choose another "
                          f"name.")}
    return None

# ── Word ─────────────────────────────────────────────────────────────────────

def word_read(path: str, max_paragraphs: int = MAX_PARAGRAPHS) -> dict:
    """Text and structure of a .docx, headings kept as headings."""
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("word",))
    if bad:
        return bad
    try:
        import docx
    except ImportError:
        return _missing("python-docx", "python-docx")
    try:
        if not os.path.isfile(path):
            return {"success": False, "error": f"Not a file: {path}"}
        document = docx.Document(path)
        blocks = []
        for para in document.paragraphs[:max(1, int(max_paragraphs))]:
            text = (para.text or "").strip()
            if not text:
                continue
            style = getattr(para.style, "name", "") or ""
            blocks.append({"style": style, "text": text,
                           "heading": style.lower().startswith("heading")})
        tables = []
        for table in document.tables:
            rows = []
            for row in table.rows:
                rows.append([(cell.text or "").strip() for cell in row.cells])
            tables.append(rows)
        return {"success": True, "path": path, "paragraphs": blocks,
                "count": len(blocks), "tables": tables,
                "tableCount": len(tables),
                "text": "\n".join(b["text"] for b in blocks)[:200000]}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def word_create(path: str, title: str = "", content: str = "",
                overwrite: bool = False) -> dict:
    """Build a .docx from plain text.

    `# `, `## `, `### ` become headings and `- `/`* ` become bullets, so the
    model can lay a document out with the plain text it is good at producing
    rather than being handed a document object it cannot serialise.
    """
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("word",))
    if bad:
        return bad
    blocked = _no_clobber(path, overwrite)
    if blocked:
        return blocked
    try:
        import docx
    except ImportError:
        return _missing("python-docx", "python-docx")
    try:
        document = docx.Document()
        if str(title or "").strip():
            document.add_heading(str(title).strip(), level=0)
        added = 0
        for raw in str(content or "").splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            stripped = line.lstrip()
            if stripped.startswith("### "):
                document.add_heading(stripped[4:].strip(), level=3)
            elif stripped.startswith("## "):
                document.add_heading(stripped[3:].strip(), level=2)
            elif stripped.startswith("# "):
                document.add_heading(stripped[2:].strip(), level=1)
            elif stripped.startswith(("- ", "* ")):
                document.add_paragraph(stripped[2:].strip(),
                                       style="List Bullet")
            else:
                document.add_paragraph(stripped)
            added += 1
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        document.save(path)
        return {"success": True, "path": path, "paragraphs": added,
                "summary": f"Created {os.path.basename(path)}"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def word_edit(path: str, find: str = "", replace: str = "",
              append: str = "", add_heading: str = "") -> dict:
    """Change a .docx in place.

    `find`/`replace` walks the paragraph's RUNS, not `paragraph.text`. A
    paragraph is stored as a list of runs, and a phrase the user sees as one
    sentence is often several runs — split by a bold word, a spell-check
    correction, or paste. Replacing against `paragraph.text` then finds nothing
    and reports success, which is the worst possible answer. The runs are joined
    to find the phrase, and the result is written into the first run.
    """
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("word",))
    if bad:
        return bad
    if not (find or append or add_heading):
        return {"success": False,
                "error": ("Nothing to change. Pass find/replace, or append, or "
                          "add_heading.")}
    try:
        import docx
    except ImportError:
        return _missing("python-docx", "python-docx")
    try:
        if not os.path.isfile(path):
            return {"success": False, "error": f"Not found: {path}"}
        document = docx.Document(path)
        replacements = 0
        # Headings and body are separate collections in python-docx, so a phrase
        # in a heading would be missed by walking paragraphs alone.
        targets = list(document.paragraphs)
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    targets.extend(cell.paragraphs)
        if find:
            for para in targets:
                if not para.runs:
                    continue
                joined = "".join(run.text for run in para.runs)
                if find not in joined:
                    continue
                new_text = joined.replace(find, replace)
                for index, run in enumerate(para.runs):
                    run.text = new_text if index == 0 else ""
                replacements += 1

        # A find that matched nothing is reported as a FAILURE, not a quiet
        # success. The document is saved either way, so returning success would
        # have the model tell the user it made a change that never happened —
        # and the file would still be rewritten, which is worse than not trying.
        if find and not replacements:
            return {"success": False, "path": path, "replacements": 0,
                    "error": (f"'{find[:80]}' was not found in "
                              f"{os.path.basename(path)}, so nothing was "
                              f"changed. Check the wording, or read the "
                              f"document first to see its exact text.")}

        if add_heading:
            document.add_heading(str(add_heading), level=1)
        appended = 0
        for line in str(append or "").splitlines():
            if line.strip():
                document.add_paragraph(line.strip())
                appended += 1
        document.save(path)
        summary = f"Updated {os.path.basename(path)}"
        if replacements:
            summary += f" ({replacements} replacement(s))"
        return {"success": True, "path": path, "replacements": replacements,
                "appended": appended, "addedHeading": bool(add_heading),
                "summary": summary}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

# ── Excel ────────────────────────────────────────────────────────────────────

def _cell(value):
    """A value a spreadsheet can hold.

    openpyxl raises on a list or dict, and the error points at the library
    rather than at the value that caused it. Coercing here keeps a nested
    structure from failing the whole write.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > MAX_CELL_VALUE:
            return value[:MAX_CELL_VALUE - 1] + "…"
        return value
    return str(value)[:MAX_CELL_VALUE]

def excel_read(path: str, sheet: str = "", max_rows: int = 500) -> dict:
    """Values from a workbook. Formulas arrive as their computed results."""
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("excel",))
    if bad:
        return bad
    try:
        import openpyxl
    except ImportError:
        return _missing("openpyxl", "openpyxl")
    try:
        if not os.path.isfile(path):
            return {"success": False, "error": f"Not found: {path}"}
        book = openpyxl.load_workbook(path, data_only=True, read_only=True)
        try:
            names = list(book.sheetnames)
            target = book[sheet] if sheet and sheet in names else book.active
            limit = max(1, min(int(max_rows), 5000))
            rows = []
            for row in target.iter_rows(values_only=True):
                rows.append(["" if v is None else v for v in row])
                if len(rows) >= limit:
                    break
            return {"success": True, "path": path, "sheets": names,
                    "sheet": target.title, "rows": rows, "rowCount": len(rows)}
        finally:
            try:
                book.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def excel_write(path: str, sheet: str = "", grid: list | None = None,
                cells: list | None = None, overwrite: bool = False) -> dict:
    """Write values into a workbook, creating it when it does not exist.

    An existing file is OPENED and updated, so this is the edit path as well as
    the create path. `overwrite` therefore means "start a new workbook instead
    of adding to this one" — without it an existing file is edited, never
    silently discarded. A plain `excel_write` on a file the user is working in
    must not throw their other sheets away.
    """
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("excel",))
    if bad:
        return bad
    if not grid and not cells:
        return {"success": False,
                "error": ("Nothing to write. Pass grid (a list of rows) or "
                          "cells ([{row, col, value}]).")}
    try:
        import openpyxl
    except ImportError:
        return _missing("openpyxl", "openpyxl")
    try:
        existed = os.path.isfile(path)
        if existed and overwrite:
            book = openpyxl.Workbook()
            target = book.active
            if sheet:
                target.title = sheet
        elif existed:
            book = openpyxl.load_workbook(path)
            target = (book[sheet] if sheet and sheet in book.sheetnames
                      else book.active)
        else:
            book = openpyxl.Workbook()
            target = book.active
            if sheet:
                target.title = sheet

        written = 0
        if isinstance(grid, (list, tuple)):
            for r, row in enumerate(grid, start=1):
                if not isinstance(row, (list, tuple)):
                    continue
                for c, value in enumerate(row, start=1):
                    target.cell(row=r, column=c, value=_cell(value))
                    written += 1
        for cell in cells or []:
            if not isinstance(cell, dict):
                continue
            r = cell.get("row")
            c = cell.get("col", cell.get("column"))
            if r is None or c is None:
                continue
            try:
                target.cell(row=int(r), column=int(c),
                            value=_cell(cell.get("value")))
                written += 1
            except (TypeError, ValueError):
                continue

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        book.save(path)
        return {"success": True, "path": path, "sheet": target.title,
                "cellsWritten": written, "created": not existed,
                "summary": (("Created " if not existed else "Updated ")
                            + os.path.basename(path))}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def excel_sheets(path: str) -> dict:
    """The sheet names and their sizes."""
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("excel",))
    if bad:
        return bad
    try:
        import openpyxl
    except ImportError:
        return _missing("openpyxl", "openpyxl")
    try:
        if not os.path.isfile(path):
            return {"success": False, "error": f"Not found: {path}"}
        book = openpyxl.load_workbook(path, read_only=True)
        try:
            out = []
            for name in book.sheetnames[:MAX_SHEETS]:
                sheet = book[name]
                out.append({"name": name,
                            "rows": getattr(sheet, "max_row", None),
                            "columns": getattr(sheet, "max_column", None)})
            return {"success": True, "path": path, "sheets": out,
                    "count": len(out)}
        finally:
            try:
                book.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

# ── PowerPoint ───────────────────────────────────────────────────────────────

def pptx_read(path: str) -> dict:
    """The title and body text of each slide."""
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("powerpoint",))
    if bad:
        return bad
    try:
        from pptx import Presentation
    except ImportError:
        return _missing("python-pptx", "python-pptx")
    try:
        if not os.path.isfile(path):
            return {"success": False, "error": f"Not found: {path}"}
        deck = Presentation(path)
        slides = []
        # `deck.slides[:n]` is NOT supported: python-pptx's slide collection
        # implements __getitem__ for an INDEX only, so a slice hands it a list
        # and it fails inside the library with "'list' object has no attribute
        # 'rId'". Iterating with a bound is the portable form.
        for index, slide in enumerate(deck.slides, start=1):
            if index > MAX_SLIDES:
                break
            title = ""
            bodies = []
            for shape in slide.shapes:
                if not getattr(shape, "has_text_frame", False):
                    continue
                text = (shape.text_frame.text or "").strip()
                if not text:
                    continue
                # The title placeholder is idx 0. `placeholder_format` raises on
                # a shape that is not a placeholder (a text box, a picture with
                # a caption), and `is_placeholder` alone is not enough — it is
                # True for every placeholder including the body, whose
                # `placeholder_format` is a list-like on some templates. Both
                # are guarded rather than assumed.
                is_title = False
                if getattr(shape, "is_placeholder", False):
                    try:
                        fmt = shape.placeholder_format
                        is_title = getattr(fmt, "idx", None) == 0
                    except (AttributeError, ValueError, TypeError):
                        is_title = False
                if is_title and not title:
                    title = text
                else:
                    bodies.append(text)
            # A deck built without the title placeholder still has a first line
            # that reads as one; report it as the title rather than losing it.
            if not title and bodies:
                title, bodies = bodies[0], bodies[1:]
            slides.append({"index": index, "title": title, "text": bodies})
        return {"success": True, "path": path, "slides": slides,
                "count": len(slides),
                "text": "\n".join(
                    f"{s['title']}\n" + "\n".join(s["text"]) for s in slides
                )[:200000]}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def pptx_create(path: str, title: str = "", slides: list | None = None,
                overwrite: bool = False) -> dict:
    """Build a .pptx from a title and a list of slides.

    Each slide is `{"title": ..., "bullets": [...]}`, or a plain string for a
    title-only slide. The layout is chosen by whether the slide has a body, so a
    section divider can be a title alone.
    """
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("powerpoint",))
    if bad:
        return bad
    blocked = _no_clobber(path, overwrite)
    if blocked:
        return blocked
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
    except ImportError:
        return _missing("python-pptx", "python-pptx")
    try:
        deck = Presentation()
        # Layout 0 is title-slide, 1 is title-and-content. Both are present in
        # the default template python-pptx ships, so this does not depend on a
        # user-supplied theme.
        title_layout = deck.slide_layouts[0]
        body_layout = deck.slide_layouts[1]

        first = deck.slides.add_slide(title_layout)
        if first.shapes.title is not None:
            first.shapes.title.text = str(title or "Presentation").strip()

        made = 0
        for item in slides or []:
            if isinstance(item, str):
                slide = deck.slides.add_slide(body_layout)
                if slide.shapes.title is not None:
                    slide.shapes.title.text = item.strip()
                made += 1
                continue
            if not isinstance(item, dict):
                continue
            bullets = [str(b).strip() for b in (item.get("bullets") or [])
                       if str(b).strip()]
            slide = deck.slides.add_slide(body_layout if bullets else title_layout)
            if slide.shapes.title is not None:
                slide.shapes.title.text = str(item.get("title") or "").strip()
            if bullets:
                body = None
                for shape in slide.placeholders:
                    if shape.placeholder_format.idx == 1:
                        body = shape
                        break
                if body is not None and body.has_text_frame:
                    frame = body.text_frame
                    frame.text = bullets[0]
                    for bullet in bullets[1:]:
                        para = frame.add_paragraph()
                        para.text = bullet
                        para.level = 0
            made += 1

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        deck.save(path)
        return {"success": True, "path": path, "slides": made + 1,
                "summary": f"Created {os.path.basename(path)}"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def pptx_add_slide(path: str, title: str = "",
                   bullets: list | None = None) -> dict:
    """Append a slide to an existing deck."""
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("powerpoint",))
    if bad:
        return bad
    try:
        from pptx import Presentation
    except ImportError:
        return _missing("python-pptx", "python-pptx")
    try:
        if not os.path.isfile(path):
            return {"success": False, "error": f"Not found: {path}"}
        deck = Presentation(path)
        lines = [str(b).strip() for b in (bullets or []) if str(b).strip()]
        layout = deck.slide_layouts[1] if lines else deck.slide_layouts[0]
        slide = deck.slides.add_slide(layout)
        if slide.shapes.title is not None:
            slide.shapes.title.text = str(title or "").strip()
        if lines:
            body = None
            for shape in slide.placeholders:
                if shape.placeholder_format.idx == 1:
                    body = shape
                    break
            if body is not None and body.has_text_frame:
                frame = body.text_frame
                frame.text = lines[0]
                for line in lines[1:]:
                    para = frame.add_paragraph()
                    para.text = line
        deck.save(path)
        return {"success": True, "path": path, "slides": len(deck.slides._sldIdLst),
                "summary": f"Added a slide to {os.path.basename(path)}"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

# ── PDF ──────────────────────────────────────────────────────────────────────

def pdf_read(path: str, pages: str = "", max_pages: int = MAX_PDF_PAGES) -> dict:
    """Text and metadata from a PDF.

    `pages` accepts "1-5", "2", or "1,3,7" — a long document is usually wanted
    in parts, and reading all of it to answer about one page wastes the context
    the answer needs.
    """
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("pdf",))
    if bad:
        return bad
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # older fallback
        except ImportError:
            return _missing("pypdf", "pypdf")
    try:
        if not os.path.isfile(path):
            return {"success": False, "error": f"Not found: {path}"}
        reader = PdfReader(path)
        total = len(reader.pages)
        wanted = _page_range(pages, total)
        if not wanted:
            return {"success": False, "path": path, "pages": total,
                    "error": (f"No page in this PDF matches {pages!r} — it has "
                              f"{total} page(s). Pass a range like '1-3', or "
                              f"omit `pages` to read all of it.")}
        wanted = wanted[:max(1, int(max_pages))]
        out = []
        for number in wanted:
            try:
                text = reader.pages[number - 1].extract_text() or ""
            except Exception as e:  # noqa: BLE001
                text = ""
                log.debug("page %s of %s could not be read: %s", number, path, e)
            out.append({"page": number, "text": text.strip()[:20000]})
        meta = {}
        try:
            raw = reader.metadata or {}
            meta = {k.lstrip("/"): str(v) for k, v in dict(raw).items()}
        except Exception as e:  # noqa: BLE001
            log.debug("no metadata in %s: %s", path, e)
        found = sum(1 for p in out if p["text"])
        result = {"success": True, "path": path, "pages": total,
                  "read": [p["page"] for p in out],
                  "content": out, "metadata": meta,
                  "text": "\n\n".join(p["text"] for p in out if p["text"])[:200000]}
        if total and not found:
            # A scanned PDF has no text layer, and reporting an empty read as a
            # success sends the model looking for a bug in its own query.
            result["note"] = ("No text could be extracted. This is most likely a "
                              "scanned or image-only PDF, which needs OCR rather "
                              "than text extraction.")
        return result
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def pdf_create(path: str, title: str = "", content: str = "",
               page_size: str = "a4", overwrite: bool = False) -> dict:
    """Build a PDF from text.

    The same markers `word_create` takes, so one convention covers both formats:
    `# `, `## `, `### ` are headings and `- `/`* ` are bullets. `---` alone on a
    line starts a new page, which is the one layout decision worth honouring —
    a section that begins halfway down a page looks like a mistake.

    Nothing here tries to reproduce a layout that already exists — this makes a
    new document from text, which is what a chat can usefully describe.

    Formatting is deliberately plain. fpdf2 ships the core fonts, and a
    non-Latin character cannot be rendered in them; rather than fail the whole
    document on one emoji, unrepeatable characters are replaced and the count is
    reported, so the caller knows the text was altered.
    """
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("pdf",))
    if bad:
        return bad
    blocked = _no_clobber(path, overwrite)
    if blocked:
        return blocked
    try:
        from fpdf import FPDF
    except ImportError:
        return _missing("fpdf2", "fpdf2")
    try:
        sizes = {"a4": "A4", "letter": "Letter", "legal": "Legal", "a3": "A3"}
        size = sizes.get(str(page_size or "a4").strip().lower(), "A4")
        pdf = FPDF(orientation="P", unit="mm", format=size)
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.set_margins(18, 18, 18)
        pdf.add_page()

        # The core fonts are latin-1. A character outside it either renders as a
        # garbled glyph or raises, depending on the version — so it is replaced
        # deliberately and counted, rather than discovered later in a corrupt
        # page.
        dropped = 0

        def clean(text: str) -> str:
            nonlocal dropped
            out_chars = []
            for ch in str(text):
                try:
                    ch.encode("latin-1")
                    out_chars.append(ch)
                except UnicodeEncodeError:
                    out_chars.append("?")
                    dropped += 1
            return "".join(out_chars)

        written = 0

        def line(style: str, size: int, text: str, height: int = 6) -> None:
            """Draw one block and leave the cursor at the left margin.

            `multi_cell` leaves `x` at the END of the line it just wrote, not
            back at the margin — so the next `multi_cell(0, ...)` computes a
            usable width of zero and raises "Not enough horizontal space to
            render a single character". Measured: after a title the cursor sat
            at 192mm of a 210mm page, and every following block failed. `ln`
            resets x to the left margin, which is what makes each call start
            where the previous one ended.
            """
            nonlocal written
            pdf.set_font("Helvetica", style, size)
            pdf.multi_cell(0, height, clean(text))
            pdf.ln(1)
            written += 1

        if str(title or "").strip():
            line("B", 18, str(title).strip(), 9)
            pdf.ln(2)

        for raw in str(content or "").splitlines():
            stripped = raw.strip()
            if not stripped:
                pdf.ln(3)
                continue
            # `---` on its own line starts a new page. Without this the whole
            # document flows onto one page and auto-breaks wherever the text
            # happens to run out of room, so a report cannot decide where a
            # section begins — which is the one layout decision a text-only
            # generator should still honour.
            if stripped in ("---", "***", "___"):
                pdf.add_page()
                continue
            if stripped.startswith("### "):
                line("B", 13, stripped[4:].strip(), 7)
            elif stripped.startswith("## "):
                line("B", 15, stripped[3:].strip(), 8)
            elif stripped.startswith("# "):
                line("B", 17, stripped[2:].strip(), 9)
            elif stripped.startswith(("- ", "* ")):
                # The bullet glyph is not in latin-1, so a hyphen stands in. A
                # copied bullet would be the first thing to corrupt the page.
                line("", 11, "- " + stripped[2:].strip())
            else:
                line("", 11, stripped)

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        pdf.output(path)
        result = {"success": True, "path": path, "blocks": written,
                   "pageSize": size,
                   "summary": f"Created {os.path.basename(path)}"}
        if dropped:
            result["replacedCharacters"] = dropped
            result["note"] = (f"{dropped} character(s) were not in the PDF core "
                              f"fonts and were written as '?'. Non-Latin text and "
                              f"emoji cannot be rendered without embedding a "
                              f"font.")
        return result
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def pdf_pages(path: str) -> dict:
    """List the pages of a PDF, with a preview of each.

    The overview to take before any page operation: rearranging or deleting
    pages by number is only safe when the numbers mean something to the caller,
    and a bare count does not tell them which page is which.
    """
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("pdf",))
    if bad:
        return bad
    try:
        from pypdf import PdfReader
    except ImportError:
        return _missing("pypdf", "pypdf")
    try:
        if not os.path.isfile(path):
            return {"success": False, "error": f"Not found: {path}"}
        reader = PdfReader(path)
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            preview = ""
            try:
                text = (page.extract_text() or "").strip()
                preview = " ".join(text.split())[:160]
            except Exception as e:  # noqa: BLE001
                log.debug("page %s of %s has no readable text: %s",
                          index, path, e)
            box = page.mediabox
            pages.append({
                "page": index,
                "preview": preview,
                "width": round(float(box.width), 1),
                "height": round(float(box.height), 1),
                "hasText": bool(preview),
            })
        return {"success": True, "path": path, "pages": pages,
                "count": len(pages)}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def _parse_pages(spec, total: int) -> list[int]:
    """Page numbers from "1-3,7", [1, 3], or "" meaning all.

    Accepts a list as well as a string because a model reliably sends
    `[1, 3]` for a schema that declares a string — the same mismatch that used
    to make `search_in_files` silently find nothing.

    Returns [] when a spec was given but matched nothing, and the caller decides
    what that means. It used to fall back to "every page", which is the wrong
    direction for a filter: asking to keep page 99 of a two-page document
    returned BOTH pages with a success message, so a request for a subset
    silently produced the whole document.
    """
    if isinstance(spec, (list, tuple)):
        out = []
        for item in spec:
            try:
                value = int(item)
            except (TypeError, ValueError):
                continue
            if 1 <= value <= total and value not in out:
                out.append(value)
        return out
    return _page_range(str(spec or ""), total)

def pdf_edit(path: str, remove_pages="", keep_pages="", order="",
             rotate: int = 0, rotate_pages="", title: str = "",
             author: str = "", subject: str = "") -> dict:
    """Change the PAGES and metadata of a PDF, in place.

    Deliberately not text editing. A PDF stores its content as positioned glyphs
    in a compressed stream, so there is no paragraph to find and replace; any
    tool claiming otherwise is either rebuilding the page or adding text on top.
    What is genuinely editable is the structure around the content — which pages
    are in the document, in what order, at what rotation, and what the metadata
    says — so that is what this does, and nothing here pretends to do more.

    Saved atomically: built beside the original and moved into place, so an
    interrupted write cannot leave a half-written PDF where a document was.
    """
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("pdf",))
    if bad:
        return bad
    if not (remove_pages or keep_pages or order or rotate
            or title or author or subject):
        return {"success": False,
                "error": ("Nothing to change. Pass remove_pages, keep_pages, "
                          "order, rotate, or metadata (title/author/subject). "
                          "Note this edits PAGES and metadata — text inside a "
                          "page cannot be rewritten.")}
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return _missing("pypdf", "pypdf")
    try:
        if not os.path.isfile(path):
            return {"success": False, "error": f"Not found: {path}"}
        reader = PdfReader(path)
        total = len(reader.pages)
        if not total:
            return {"success": False,
                    "error": f"{os.path.basename(path)} has no pages."}

        changes = []

        # `keep_pages` is expressed as the pages to KEEP, which is clearer than
        # a removal list when the caller is thinking about the result.
        if keep_pages:
            keep = _parse_pages(keep_pages, total)
            dropped = total - len(keep)
            if not keep:
                return {"success": False,
                        "error": "keep_pages matched no pages, so the document "
                                 "would be empty."}
            changes.append(f"kept {len(keep)} of {total} page(s)")
        else:
            remove = _parse_pages(remove_pages, total) if remove_pages else []
            if remove_pages and not remove:
                # A removal list that matched nothing would report success and
                # change nothing, which reads as "done" to whoever asked.
                return {"success": False,
                        "error": (f"remove_pages ({remove_pages!r}) matched no "
                                  f"page — this PDF has {total} page(s).")}
            keep = [n for n in range(1, total + 1) if n not in remove]
            if not keep:
                return {"success": False,
                        "error": ("Every page would be removed. A PDF with no "
                                  "pages cannot be saved — remove the file "
                                  "instead if that is what you meant.")}
            dropped = len(remove)
            if remove:
                changes.append(f"removed {len(remove)} page(s)")

        # `order` is a full page list in the wanted sequence; it may also omit
        # pages, in which case those are dropped, which the summary reports.
        if order:
            wanted = _parse_pages(order, total)
            sequence = [n for n in wanted if n in keep]
            if not sequence:
                return {"success": False,
                        "error": ("order describes no page that is still in the "
                                  "document.")}
            if sequence != keep:
                changes.append(f"reordered to {sequence}")
            keep = sequence

        if rotate:
            try:
                angle = int(rotate) % 360
            except (TypeError, ValueError):
                return {"success": False,
                        "error": f"rotate must be a number of degrees, not "
                                 f"{rotate!r}."}
            if angle % 90:
                return {"success": False,
                        "error": ("rotate must be a multiple of 90 — a PDF page "
                                  "is turned, not skewed.")}
            which = _parse_pages(rotate_pages, total) if rotate_pages else keep
            if not which:
                return {"success": False,
                        "error": (f"rotate_pages ({rotate_pages!r}) matched no "
                                  f"page — this PDF has {total} page(s).")}
            changes.append(f"rotated {len(which)} page(s) by {angle}°")
        else:
            angle, which = 0, []

        writer = PdfWriter()
        for number in keep:
            page = reader.pages[number - 1]
            if angle and number in which:
                page.rotate(angle)
            writer.add_page(page)

        meta = {}
        try:
            existing = reader.metadata or {}
            for key in ("/Title", "/Author", "/Subject", "/Creator"):
                if existing.get(key):
                    meta[key] = str(existing[key])
        except Exception as e:  # noqa: BLE001
            log.debug("no metadata to carry over from %s: %s", path, e)
        if title:
            meta["/Title"] = title
            changes.append("set the title")
        if author:
            meta["/Author"] = author
            changes.append("set the author")
        if subject:
            meta["/Subject"] = subject
            changes.append("set the subject")
        if meta:
            writer.add_metadata(meta)

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            writer.write(fh)
        os.replace(tmp, path)
        return {"success": True, "path": path, "pages": len(keep),
                "pagesBefore": total, "pagesRemoved": dropped,
                "changes": changes,
                "summary": (f"Updated {os.path.basename(path)}: "
                            + ", ".join(changes))}
    except Exception as e:  # noqa: BLE001
        try:
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")
        except OSError:
            pass
        return {"success": False, "error": str(e)}

def pdf_merge(path: str, sources: list | None = None, keep_source: bool = True,
              overwrite: bool = False) -> dict:
    """Combine PDFs into one, in the order given.

    `sources` are joined onto `path` in sequence, so `pdf_merge` doubles as
    "append these files to that one". The sources are kept by default: a merge
    that silently consumed its inputs would destroy documents the caller may
    have meant to reuse, and the same file can legitimately be merged into two
    different reports.
    """
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("pdf",))
    if bad:
        return bad
    listed = [str(s).strip() for s in (sources or []) if str(s).strip()]
    if not listed:
        return {"success": False,
                "error": ("Nothing to merge. Pass sources as a list of PDF "
                          "paths.")}
    resolved = []
    for item in listed:
        target, refusal = _guard(item)
        if refusal:
            return refusal
        bad = _require(target, ("pdf",))
        if bad:
            return bad
        if not os.path.isfile(target):
            return {"success": False,
                    "error": f"Not found: {item}"}
        resolved.append(target)
    if os.path.exists(path) and not overwrite:
        return {"success": False, "blocked": True, "path": path,
                "error": (f"{os.path.basename(path)} already exists. Pass "
                          f"overwrite=true to replace it, or name a new file.")}
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return _missing("pypdf", "pypdf")
    try:
        writer = PdfWriter()
        # An existing destination is the base; otherwise the first source is.
        bases = [path] if os.path.isfile(path) else []
        added = 0
        for item in bases + resolved:
            reader = PdfReader(item)
            for page in reader.pages:
                writer.add_page(page)
                added += 1
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            writer.write(fh)
        os.replace(tmp, path)
        if not keep_source:
            # Only the sources, never the destination.
            for item in resolved:
                if os.path.abspath(item) != os.path.abspath(path):
                    try:
                        os.remove(item)
                    except OSError as e:  # noqa: BLE001
                        log.warning("could not remove %s after merging: %s",
                                    item, e)
        return {"success": True, "path": path, "pages": added,
                "merged": len(resolved) + len(bases),
                "sourcesKept": bool(keep_source),
                "summary": f"Merged into {os.path.basename(path)} "
                           f"({added} pages)"}
    except Exception as e:  # noqa: BLE001
        try:
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")
        except OSError:
            pass
        return {"success": False, "error": str(e)}

def pdf_extract(path: str, pages: str = "", output: str = "",
                overwrite: bool = False) -> dict:
    """Pull pages out of a PDF into a new file.

    Kept separate from `pdf_edit` with keep_pages because the intent differs:
    this reads one document and writes another, leaving the original alone,
    which is what someone asking to "extract pages 3-5" expects.
    """
    source, refused = _guard(path)
    if refused:
        return refused
    bad = _require(source, ("pdf",))
    if bad:
        return bad
    if not str(output or "").strip():
        base = os.path.splitext(source)[0]
        output = f"{base}_extract.pdf"
    dest, refusal = _guard(output)
    if refusal:
        return refusal
    bad = _require(dest, ("pdf",))
    if bad:
        return bad
    blocked = _no_clobber(dest, overwrite)
    if blocked:
        return blocked
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return _missing("pypdf", "pypdf")
    try:
        if not os.path.isfile(source):
            return {"success": False, "error": f"Not found: {path}"}
        reader = PdfReader(source)
        total = len(reader.pages)
        wanted = _parse_pages(pages, total) if pages else list(range(1, total + 1))
        if not wanted:
            return {"success": False,
                    "error": f"No page in {os.path.basename(source)} matches "
                             f"{pages!r} (it has {total} page(s))."}
        writer = PdfWriter()
        for number in wanted:
            writer.add_page(reader.pages[number - 1])
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as fh:
            writer.write(fh)
        os.replace(tmp, dest)
        return {"success": True, "path": dest, "source": source,
                "pages": wanted, "count": len(wanted),
                "summary": (f"Extracted {len(wanted)} page(s) from "
                            f"{os.path.basename(source)} into "
                            f"{os.path.basename(dest)}")}
    except Exception as e:  # noqa: BLE001
        try:
            if os.path.exists(dest + ".tmp"):
                os.remove(dest + ".tmp")
        except OSError:
            pass
        return {"success": False, "error": str(e)}

def _page_range(spec: str, total: int) -> list[int]:
    """1-based page numbers from "1-5", "2", "1,3,7" or "" (meaning all).

    An EMPTY spec means every page; a spec that matches nothing returns []. The
    two are different answers and the distinction matters — treating "page 99 of
    a 2-page file" as "all pages" turns a request for a subset into a silent
    no-op that reports success.
    """
    text = str(spec or "").strip()
    if not text:
        return list(range(1, total + 1))
    numbers: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            try:
                start, end = int(low), int(high)
            except ValueError:
                continue
            if start > end:
                start, end = end, start
            numbers.extend(range(max(1, start), min(total, end) + 1))
        else:
            try:
                value = int(part)
            except ValueError:
                continue
            if 1 <= value <= total:
                numbers.append(value)
    # Deduplicated but in the order asked for, so "3,1" reads 3 then 1.
    seen = set()
    ordered = []
    for number in numbers:
        if number not in seen:
            seen.add(number)
            ordered.append(number)
    return ordered

# ── PDF redaction ────────────────────────────────────────────────────────────
#
# Real redaction, which is NOT drawing a black box. A rectangle over text leaves
# the glyphs in the content stream, so the "redacted" words are still there for
# anyone who copies the page or runs text extraction — a security failure that
# looks like success. Verified on this machine: text with a box drawn over it
# still came back from extract_text().
#
# Removing text from a content stream needs a library that rewrites it. `pypdf`
# can read and draw but not excise, so this uses PyMuPDF, whose `apply_redactions`
# genuinely deletes the covered glyphs. Confirmed: after redacting an account
# number it no longer appears in the extracted text, while the surrounding line
# does.

def _fitz():
    """Import PyMuPDF under its current name.

    `import fitz` still works but warns it will be removed, and a warning on
    every call is noise in the log for a feature that is working.
    """
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        try:
            import fitz  # older name
            return fitz
        except ImportError:
            return None

def pdf_redact(path: str, terms: list | str | None = None,
               pages: str = "", output: str = "",
               overwrite: bool = False) -> dict:
    """Permanently remove text from a PDF.

    `terms` are literal strings to find and delete — an account number, a name,
    an address. The text is REPLACED, not covered, so it cannot be recovered by
    copying the page or extracting the text.

    The layout is kept: redaction rewrites only the content it covers, so the
    rest of the document is untouched.

    Writes to a NEW file rather than editing in place. Redaction cannot be
    undone, so the result is something to look at before it replaces anything —
    and the answer reports exactly how many occurrences went, because
    "redacted" with nothing removed is the failure worth catching.
    """
    source, refused = _guard(path)
    if refused:
        return refused
    bad = _require(source, ("pdf",))
    if bad:
        return bad

    wanted = [terms] if isinstance(terms, str) else list(terms or [])
    wanted = [str(t).strip() for t in wanted if str(t).strip()]
    if not wanted:
        return {"success": False,
                "error": ("Nothing to redact. Pass terms — the exact strings to "
                          "remove, e.g. [\"4111-2222-3333\"].")}

    if not str(output or "").strip():
        output = os.path.splitext(source)[0] + "_redacted.pdf"
    dest, refusal = _guard(output)
    if refusal:
        return refusal
    bad = _require(dest, ("pdf",))
    if bad:
        return bad
    if os.path.abspath(dest) == os.path.abspath(source):
        return {"success": False,
                "error": ("Redact to a different file. Redaction cannot be "
                          "undone, so overwriting the original leaves nothing "
                          "to check the result against.")}
    # The destination is a NEW file by default, but a caller can pass an
    # explicit `output` that already exists. Refuse that unless asked, the same
    # as every other operation here, so a guess cannot destroy a document.
    clobbered = _no_clobber(dest, overwrite)
    if clobbered:
        return clobbered

    pymupdf = _fitz()
    if pymupdf is None:
        return _missing("PyMuPDF", "pymupdf")

    try:
        if not os.path.isfile(source):
            return {"success": False, "error": f"Not found: {path}"}
        doc = pymupdf.open(source)
        total = len(doc)
        targets = _parse_pages(pages, total) if pages else list(range(1, total + 1))
        if not targets:
            doc.close()
            return {"success": False,
                    "error": (f"pages ({pages!r}) matched no page — this PDF "
                              f"has {total} page(s).")}

        per_term = {term: 0 for term in wanted}
        per_page = {}
        for number in targets:
            page = doc[number - 1]
            found_here = 0
            for term in wanted:
                # search_for is literal, so a value containing a full stop is
                # matched as written rather than as a pattern.
                try:
                    hits = page.search_for(term)
                except Exception as e:  # noqa: BLE001
                    log.debug("search failed for %r: %s", term, e)
                    continue
                for rect in hits:
                    page.add_redact_annot(rect)
                    per_term[term] += 1
                    found_here += 1
            if found_here:
                per_page[number] = found_here
                # images=2 keeps an image that only partly overlaps, so
                # redacting a line of text does not delete a logo behind it.
                page.apply_redactions(images=2, graphics=1)

        removed = sum(per_term.values())
        if not removed:
            doc.close()
            return {"success": False, "source": source,
                    "error": (f"None of {wanted} was found, so nothing was "
                              f"redacted. Check the exact wording, or read the "
                              f"document first to see the text as it is stored.")}

        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".tmp"
        # PyMuPDF refuses to write over the file it has open unless the save is
        # incremental, which conveniently forces the write-then-replace this
        # wants anyway.
        doc.save(tmp, garbage=4, deflate=True)
        doc.close()
        os.replace(tmp, dest)

        missing = [t for t, n in per_term.items() if not n]
        result = {"success": True, "path": dest, "source": source,
                  "redacted": removed, "perTerm": per_term,
                  "perPage": per_page, "pagesChecked": len(targets),
                  "summary": (f"Redacted {removed} occurrence(s) into "
                              f"{os.path.basename(dest)}")}
        if missing:
            # Reported, not a failure: a term may legitimately be absent, but a
            # term that matched nothing is what the user most needs to know.
            result["notFound"] = missing
            result["note"] = (f"Not found anywhere: {', '.join(missing)}. They "
                              f"may be worded differently, or split across a "
                              f"line break.")
        return result
    except Exception as e:  # noqa: BLE001
        try:
            if os.path.exists(dest + ".tmp"):
                os.remove(dest + ".tmp")
        except OSError:
            pass
        return {"success": False, "error": str(e)}

def pdf_redact_verify(path: str, terms: list | str | None = None) -> dict:
    """Check that the given text can no longer be extracted from a PDF.

    The check that makes a redaction claim falsifiable. "Redacted" is only true
    if the text is gone from the extractable content; if it still comes back
    from a text search then the redaction did not work, whatever the page looks
    like. Run this after `pdf_redact` when the document matters — a covered
    glyph is indistinguishable from a removed one until you search for it.
    """
    path, refused = _guard(path)
    if refused:
        return refused
    bad = _require(path, ("pdf",))
    if bad:
        return bad
    wanted = [terms] if isinstance(terms, str) else list(terms or [])
    wanted = [str(t).strip() for t in wanted if str(t).strip()]
    if not wanted:
        return {"success": False,
                "error": "Pass the terms to look for, e.g. [\"4111-2222-3333\"]."}
    try:
        from pypdf import PdfReader
    except ImportError:
        return _missing("pypdf", "pypdf")
    try:
        if not os.path.isfile(path):
            return {"success": False, "error": f"Not found: {path}"}
        reader = PdfReader(path)
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        lowered = text.casefold()
        still = [t for t in wanted if t.casefold() in lowered]
        return {"success": not still, "path": path, "pages": len(reader.pages),
                "clean": not still, "stillPresent": still,
                "note": ("No trace of the given term(s) in the extractable text."
                         if not still else
                         f"STILL EXTRACTABLE: {', '.join(still)}. The text was "
                         f"not actually removed.")}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

# ── Conversion ───────────────────────────────────────────────────────────────

def convert_to_pdf(path: str, output: str = "",
                   overwrite: bool = False) -> dict:
    """Convert a document to PDF, keeping its layout.

    Word and PowerPoint go through PyMuPDF's own converter, which reads the
    Office file and lays it out — so headings, fonts and slides survive instead
    of being flattened into a wall of text.

    Excel is rendered HERE rather than by that converter. Measured on this
    machine, the library's own xlsx path dropped every TEXT cell and kept only
    the numbers: a sheet reading [Item, Cost, Hosting, 120] produced a PDF
    containing "120" and nothing else. That is worse than refusing, because the
    output looks like a successful conversion. The sheet is read with openpyxl
    and written as a table instead, which keeps the data and loses the styling
    — and the answer says so.
    """
    source, refused = _guard(path)
    if refused:
        return refused
    try:
        if not os.path.isfile(source):
            return {"success": False, "error": f"Not found: {path}"}
    except OSError as e:
        return {"success": False, "error": str(e)}

    kind = _kind(source)
    ext = os.path.splitext(source)[1].lower()
    if not str(output or "").strip():
        output = os.path.splitext(source)[0] + ".pdf"
    dest, refusal = _guard(output)
    if refusal:
        return refusal
    bad = _require(dest, ("pdf",))
    if bad:
        return bad
    blocked = _no_clobber(dest, overwrite)
    if blocked:
        return blocked
    if os.path.abspath(dest) == os.path.abspath(source):
        return {"success": False,
                "error": "The destination is the source. Give a different name."}

    if kind == "excel" or ext in (".xlsx", ".xlsm"):
        return _xlsx_to_pdf(source, dest)

    if ext == ".pptx":
        return _pptx_to_pdf(source, dest)

    if kind not in ("word", "powerpoint") and ext not in (".txt", ".md", ".csv"):
        return {"success": False,
                "error": (f"{ext or 'that file'} cannot be converted to PDF. "
                          f"Supported: .docx, .pptx, .xlsx, .txt, .md, .csv.")}

    pymupdf = _fitz()
    if pymupdf is None:
        return _missing("PyMuPDF", "pymupdf")
    try:
        doc = pymupdf.open(source)
        try:
            pages = len(doc)
            data = doc.convert_to_pdf()
        finally:
            doc.close()
        if not isinstance(data, (bytes, bytearray)):
            # Some versions hand back an open document instead of bytes.
            with pymupdf.open("pdf", data) as converted:
                data = converted.tobytes()
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
        return {"success": True, "path": dest, "source": source,
                "sourcePages": pages, "from": ext,
                "summary": (f"Converted {os.path.basename(source)} to "
                            f"{os.path.basename(dest)}")}
    except Exception as e:  # noqa: BLE001
        try:
            if os.path.exists(dest + ".tmp"):
                os.remove(dest + ".tmp")
        except OSError:
            pass
        return {"success": False,
                "error": (f"Could not convert {os.path.basename(source)}: {e}. "
                          f"Office conversion reads the file's layout, which "
                          f"needs a well-formed document.")}

def convert_from_pdf(path: str, output: str = "", to: str = "text",
                     overwrite: bool = False) -> dict:
    """Turn a PDF into text, HTML or a Word document.

    Text and HTML come from the PDF's own text layer, so a scanned PDF produces
    nothing and is reported as such rather than written out empty.

    `docx` builds a Word file with each page's text as paragraphs. That is an
    EXTRACTION, not a faithful rebuild: a PDF's layout is positional, and
    expressing it as a flowing document throws the positions away. Tables become
    runs of lines. Saying so is better than producing a Word file that looks
    subtly wrong and leaving the caller to work out why.
    """
    source, refused = _guard(path)
    if refused:
        return refused
    bad = _require(source, ("pdf",))
    if bad:
        return bad
    target = str(to or "text").strip().lower()
    if target in ("txt", "plain"):
        target = "text"
    if target in ("word", "doc"):
        target = "docx"
    if target not in ("text", "html", "docx"):
        return {"success": False,
                "error": (f"'to' must be text, html or docx — got {to!r}.")}
    if not str(output or "").strip():
        output = os.path.splitext(source)[0] + {
            "text": ".txt", "html": ".html", "docx": ".docx"}[target]
    dest, refusal = _guard(output)
    if refusal:
        return refusal
    want = ({"docx": "word", "text": "", "html": ""}[target])
    if want:
        bad = _require(dest, (want,))
        if bad:
            return bad
    blocked = _no_clobber(dest, overwrite)
    if blocked:
        return blocked

    try:
        if not os.path.isfile(source):
            return {"success": False, "error": f"Not found: {path}"}

        if target == "docx":
            return _pdf_to_docx(source, dest)

        pymupdf = _fitz()
        if pymupdf is None:
            return _missing("PyMuPDF", "pymupdf")
        doc = pymupdf.open(source)
        try:
            pages = len(doc)
            if target == "html":
                body = []
                for index, page in enumerate(doc, start=1):
                    body.append(f"<section class=\"page\" data-page=\"{index}\">")
                    # The library's own HTML writer keeps paragraphs and simple
                    # styling, which is far closer to the page than the plain
                    # text would be.
                    body.append(page.get_text("html"))
                    body.append("</section>")
                content = ("<!doctype html>\n<html><head><meta charset=\"utf-8\">"
                           "<title>" + os.path.basename(source) + "</title></head>"
                           "<body>\n" + "\n".join(body) + "\n</body></html>\n")
            else:
                content = "\n\n".join(
                    f"--- page {i} ---\n{page.get_text()}"
                    for i, page in enumerate(doc, start=1))
        finally:
            doc.close()

        stripped = content.strip()
        if not stripped or (target == "text"
                            and not stripped.replace("-", "").strip()):
            return {"success": False, "source": source, "pages": pages,
                    "error": ("No text could be extracted, so there is nothing "
                              "to convert. This is most likely a scanned or "
                              "image-only PDF, which needs OCR.")}

        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
        return {"success": True, "path": dest, "source": source, "pages": pages,
                "format": target, "characters": len(content),
                "summary": (f"Converted {os.path.basename(source)} to "
                            f"{os.path.basename(dest)}")}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def _pdf_to_docx(source: str, dest: str) -> dict:
    """PDF text into a Word document, page by page."""
    try:
        import docx
    except ImportError:
        return _missing("python-docx", "python-docx")
    pymupdf = _fitz()
    if pymupdf is None:
        return _missing("PyMuPDF", "pymupdf")
    try:
        doc = pymupdf.open(source)
        try:
            pages = len(doc)
            document = docx.Document()
            document.add_heading(os.path.splitext(
                os.path.basename(source))[0], level=0)
            written = 0
            for index, page in enumerate(doc, start=1):
                text = (page.get_text() or "").strip()
                if index > 1:
                    document.add_page_break()
                document.add_heading(f"Page {index}", level=2)
                if not text:
                    document.add_paragraph("(no text on this page)")
                    continue
                for para in text.split("\n"):
                    if para.strip():
                        document.add_paragraph(para.strip())
                        written += 1
        finally:
            doc.close()
        if not written:
            return {"success": False, "source": source, "pages": pages,
                    "error": ("No text could be extracted, so the Word file "
                              "would be empty. This is most likely a scanned "
                              "PDF, which needs OCR.")}
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        document.save(dest)
        return {"success": True, "path": dest, "source": source, "pages": pages,
                "paragraphs": written, "format": "docx",
                "note": ("The text is extracted, not rebuilt: a PDF stores "
                         "positioned glyphs, so layout, tables and columns do "
                         "not survive being expressed as a flowing document."),
                "summary": (f"Converted {os.path.basename(source)} to "
                            f"{os.path.basename(dest)}")}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def _clean_pdf_text(value) -> str:
    """Text the PDF core fonts can hold, with unrenderable characters marked.

    fpdf2 ships the core fonts and they are latin-1; a character outside that
    either draws as a garbled glyph or raises, depending on the version. It is
    replaced deliberately here so a conversion never fails on one emoji, and the
    caller is told how many were replaced.
    """
    out = []
    for ch in str(value if value is not None else ""):
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)

def _pptx_to_pdf(source: str, dest: str) -> dict:
    """Render a deck as a PDF, ONE PAGE PER SLIDE.

    PyMuPDF's own converter stacks every slide onto a single page — measured
    with a 3-slide deck: one 400x600 page containing "One\\nTwo\\nThree". The
    text is all there, so it looks like a successful conversion, but a deck
    whose slides are not pages is not a usable deck. Slides are therefore read
    with python-pptx and laid out here.

    This keeps the structure and loses the theme: text, order and titles
    survive, the fonts and background artwork do not.
    """
    try:
        from pptx import Presentation
    except ImportError:
        return _missing("python-pptx", "python-pptx")
    try:
        from fpdf import FPDF
    except ImportError:
        return _missing("fpdf2", "fpdf2")
    try:
        deck = Presentation(source)
        slides = []
        for slide in deck.slides:
            title = ""
            bodies = []
            for shape in slide.shapes:
                if not getattr(shape, "has_text_frame", False):
                    continue
                text = (shape.text_frame.text or "").strip()
                if not text:
                    continue
                is_title = False
                if getattr(shape, "is_placeholder", False):
                    try:
                        is_title = getattr(
                            shape.placeholder_format, "idx", None) == 0
                    except (AttributeError, ValueError, TypeError):
                        is_title = False
                if is_title and not title:
                    title = text
                else:
                    bodies.append(text)
            if not title and bodies:
                title, bodies = bodies[0], bodies[1:]
            slides.append((title, bodies))

        if not slides:
            return {"success": False,
                    "error": f"{os.path.basename(source)} has no slides."}

        replaced = 0

        def clean(value) -> str:
            nonlocal replaced
            text = _clean_pdf_text(value)
            replaced += sum(1 for a, b in
                            zip(str(value if value is not None else ""), text)
                            if a != b)
            return text

        # A landscape page the shape of a slide, so the result reads as a deck
        # rather than as a document with wide margins.
        pdf = FPDF(orientation="L", unit="mm", format="A4")
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.set_margins(15, 15, 15)
        written = 0
        for index, (title, bodies) in enumerate(slides, start=1):
            pdf.add_page()
            written += 1
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_x(15)
            pdf.multi_cell(0, 5, f"Slide {index}")
            pdf.ln(1)
            if title:
                pdf.set_font("Helvetica", "B", 20)
                pdf.set_x(15)
                pdf.multi_cell(0, 11, clean(title))
                pdf.ln(2)
            pdf.set_font("Helvetica", "", 12)
            for body in bodies:
                pdf.set_x(15)
                for line in body.splitlines():
                    if line.strip():
                        # The bullet glyph is not latin-1; a hyphen stands in.
                        pdf.set_x(15)
                        pdf.multi_cell(0, 7, "- " + clean(line.strip()))
                pdf.ln(1)

        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        pdf.output(dest)
        result = {"success": True, "path": dest, "source": source,
                  "slides": written, "pages": written,
                  "summary": (f"Converted {os.path.basename(source)} "
                              f"({written} slide(s)) to PDF"),
                  "note": ("One page per slide. Text, titles and order are "
                           "kept; the deck's theme, fonts and background "
                           "artwork are not. The library's own converter put "
                           "every slide on a single page.")}
        if replaced:
            result["replacedCharacters"] = replaced
        return result
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}

def _xlsx_to_pdf(source: str, dest: str) -> dict:
    """Render a workbook as a PDF table, one section per sheet.

    The trade-off is reported rather than hidden: every cell VALUE is kept and
    the cell formatting is not.
    """
    try:
        import openpyxl
    except ImportError:
        return _missing("openpyxl", "openpyxl")
    try:
        book = openpyxl.load_workbook(source, data_only=True)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}
    try:
        try:
            from fpdf import FPDF
        except ImportError:
            return _missing("fpdf2", "fpdf2")

        def clean(value) -> str:
            text = "" if value is None else str(value)
            out = []
            for ch in text:
                try:
                    ch.encode("latin-1")
                    out.append(ch)
                except UnicodeEncodeError:
                    out.append("?")
            return "".join(out)

        pdf = FPDF(orientation="L", unit="mm", format="A4")
        pdf.set_auto_page_break(auto=True, margin=12)
        pdf.set_margins(12, 12, 12)
        sheets = 0
        rows_written = 0
        truncated = False

        for name in book.sheetnames[:MAX_SHEETS]:
            pdf.add_page()
            sheets += 1
            pdf.set_font("Helvetica", "B", 13)
            pdf.set_x(12)
            pdf.multi_cell(0, 8, clean(name))
            pdf.ln(1)

            rows = list(book[name].iter_rows(values_only=True))
            if not rows:
                pdf.set_font("Helvetica", "I", 10)
                pdf.set_x(12)
                pdf.multi_cell(0, 6, "(empty sheet)")
                continue
            if len(rows) > 500:
                rows = rows[:500]
                truncated = True

            # Column widths sized to the content, so a table of short values is
            # not rendered as one narrow strip down the page.
            widths = []
            for col in range(max(len(r) for r in rows)):
                widest = 6
                for row in rows[:200]:
                    if col < len(row):
                        widest = max(widest, len(clean(row[col])) + 2)
                widths.append(min(widest, 48))

            for index, row in enumerate(rows):
                pdf.set_font("Helvetica", "B" if index == 0 else "", 9)
                pdf.set_x(12)
                for col, value in enumerate(row):
                    width = widths[col] if col < len(widths) else 20
                    pdf.cell(width, 6, clean(value)[:80], border=1)
                pdf.ln(6)
                rows_written += 1

        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        pdf.output(dest)
        note = ("Cells are rendered as a table: every value is kept, cell "
                "formatting and formulas are not. The library's own spreadsheet "
                "converter dropped text cells entirely, so the sheet is read "
                "instead.")
        if truncated:
            note += " Only the first 500 rows of each sheet were written."
        return {"success": True, "path": dest, "source": source,
                "sheets": sheets, "rows": rows_written, "note": note,
                "summary": (f"Converted {os.path.basename(source)} "
                            f"({sheets} sheet(s)) to PDF")}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass
