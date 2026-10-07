"""Add click-to-open pictures to a paper PDF.

A paper PDF goes in. The same PDF comes out. Figure and scheme mentions are
highlighted in yellow, and chemical names in green. Clicking a highlight
opens the picture beside the sentence. It does not jump to another page.
A card opens beside the word, with its name on top. Arrow buttons move the card. The close button hides it.

The picture script runs in Adobe Acrobat. The highlights are visible in
every viewer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import pymupdf

Progress = Callable[[str, float], None]


LABELS = {
    "figure": "figure",
    "fig": "figure",
    "figs": "figure",
    "figures": "figure",
    "scheme": "scheme",
    "schemes": "scheme",
}

_NUMBER = re.compile(r"(\d{1,3})([A-Za-z])?$")
_RANGE = re.compile(r"(\d{1,3})\s*[-–—]\s*(\d{1,3})$")


@dataclass
class Figure:
    key: str
    page: int
    clip: pymupdf.Rect
    png: bytes
    aspect: float
    caption_png: bytes
    caption_aspect: float
    panels: dict[str, tuple[bytes, float]]


@dataclass
class Reference:
    page: int
    rect: pymupdf.Rect
    key: str
    label: str
    panel: str = ""


def _clean_token(word: str) -> str:
    return word.strip("()[]{}").rstrip(".,;:")


def _label_kind(word: str) -> str | None:
    token = _clean_token(word).lower().rstrip(".")
    return LABELS.get(token)


def _numbers(word: str) -> list[tuple[str, str]]:
    """Return (figure number, panel letter). The letter is empty when the mention is the whole figure."""
    token = _clean_token(word)
    ranged = _RANGE.fullmatch(token)
    if ranged:
        start, end = int(ranged.group(1)), int(ranged.group(2))
        if 0 < end - start <= 6:
            return [(str(number), "") for number in range(start, end + 1)]
        return [(ranged.group(1), "")]
    wrapped = re.fullmatch(r"(\d{1,3})\(([A-Za-z])\)", token)
    if wrapped:
        return [(wrapped.group(1), wrapped.group(2).upper())]
    matched = _NUMBER.fullmatch(token)
    if matched:
        return [(matched.group(1), (matched.group(2) or "").upper())]
    return []


def _panel_token(word: str) -> str:
    token = _clean_token(word)
    match = re.fullmatch(r"[\(\[]?([A-Ha-h])[\)\].]?", token)
    if match and len(token) <= 4:
        return match.group(1).upper()
    return ""


def _lines(page: pymupdf.Page) -> list[list[tuple]]:
    parent = page.parent
    key = (id(parent), page.number) if parent is not None else None
    cache: dict = _lines.__dict__.setdefault("cache", {})
    if key is not None and key in cache:
        return cache[key]
    grouped: dict[tuple[int, int], list[tuple]] = {}
    for word in page.get_text("words"):
        grouped.setdefault((word[5], word[6]), []).append(word)
    lines = []
    for words in grouped.values():
        words.sort(key=lambda item: item[7])
        lines.append(words)
    lines.sort(key=lambda words: (words[0][1], words[0][0]))
    if key is not None:
        cache[key] = lines
    return lines


def _drop_lines(doc: pymupdf.Document) -> None:
    cache: dict = _lines.__dict__.get("cache") or {}
    marker = id(doc)
    for key in [item for item in cache if item[0] == marker]:
        del cache[key]


def _union(words: list[tuple]) -> pymupdf.Rect:
    rect = pymupdf.Rect(words[0][:4])
    for word in words[1:]:
        rect |= pymupdf.Rect(word[:4])
    return rect


def _column(rect: pymupdf.Rect, page: pymupdf.Rect) -> pymupdf.Rect:
    margin = 36
    if rect.width > page.width * 0.55:
        return pymupdf.Rect(margin, page.y0, page.width - margin, page.y1)
    mid = page.width / 2
    if (rect.x0 + rect.x1) / 2 < mid:
        return pymupdf.Rect(margin, page.y0, mid - 8, page.y1)
    return pymupdf.Rect(mid + 8, page.y0, page.width - margin, page.y1)


def _mostly_blank(pixmap: pymupdf.Pixmap) -> bool:
    data = pixmap.samples
    channels = pixmap.n
    if not data or channels < 3:
        return True
    step = max(channels, len(data) // 5000 // channels * channels)
    dark = 0
    total = 0
    for index in range(0, len(data) - channels, step):
        total += 1
        if data[index] < 242 or data[index + 1] < 242 or data[index + 2] < 242:
            dark += 1
    return total == 0 or dark / total < 0.012


def _render_clip(page: pymupdf.Page, clip: pymupdf.Rect) -> bytes | None:
    if clip.width < 20 or clip.height < 20:
        return None
    if clip.height > 480:
        clip = pymupdf.Rect(clip.x0, clip.y1 - 480, clip.x1, clip.y1)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), clip=clip, alpha=False)
    if _mostly_blank(pixmap):
        return None
    return pixmap.tobytes("png")


def _caption_span(page: pymupdf.Page, caption: pymupdf.Rect, column: pymupdf.Rect) -> pymupdf.Rect:
    """The caption line plus the lines that continue it."""
    block = caption
    previous = caption.y1
    extras = 0
    for line in _lines(page):
        rect = _union(line)
        if rect.y0 < caption.y0 - 1:
            continue
        if abs(rect.y0 - caption.y0) < 2 and rect.intersects(caption):
            continue
        if rect.y0 - previous > 11 or extras >= 5:
            break
        if rect.x1 < column.x0 or rect.x0 > column.x1:
            continue
        kind = _label_kind(line[0][4])
        if kind and rect.y0 > caption.y1:
            break
        block |= rect
        previous = rect.y1
        extras += 1
    return block


def _label_cells(page: pymupdf.Page, clip: pymupdf.Rect) -> dict[str, pymupdf.Rect]:
    """Panel letters printed on the figure, each owning its cell."""
    found: dict[str, pymupdf.Rect] = {}
    for word in page.get_text("words"):
        rect = pymupdf.Rect(word[:4])
        center = pymupdf.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
        if not clip.contains(center) or rect.width > 28 or rect.height > 20:
            continue
        letter = _panel_token(word[4])
        if not letter:
            continue
        if letter not in found or rect.y0 < found[letter].y0:
            found[letter] = rect
    if len(found) < 2:
        return {}
    ordered = sorted(found.items(), key=lambda item: (item[1].y0, item[1].x0))
    rows: list[list[tuple[str, pymupdf.Rect]]] = []
    for letter, rect in ordered:
        if rows and abs(rect.y0 - rows[-1][0][1].y0) < 16:
            rows[-1].append((letter, rect))
        else:
            rows.append([(letter, rect)])
    cells: dict[str, pymupdf.Rect] = {}
    for row_index, row in enumerate(rows):
        row.sort(key=lambda item: item[1].x0)
        for column_index, (letter, rect) in enumerate(row):
            if column_index == 0:
                left = clip.x0
            else:
                left = (row[column_index - 1][1].x1 + rect.x0) / 2
            if column_index + 1 == len(row):
                right = clip.x1
            else:
                right = (rect.x1 + row[column_index + 1][1].x0) / 2
            if row_index == 0:
                top = clip.y0
            else:
                top = (rows[row_index - 1][0][1].y1 + rect.y0) / 2
            if row_index + 1 == len(rows):
                bottom = clip.y1
            else:
                bottom = (rect.y1 + rows[row_index + 1][0][1].y0) / 2
            cell = pymupdf.Rect(left, top, right, bottom) & clip
            if cell.width > 16 and cell.height > 16:
                cells[letter] = cell
    return cells


def _gutter_cells(page: pymupdf.Page, clip: pymupdf.Rect) -> dict[str, pymupdf.Rect]:
    """Split a figure on the white gaps between panels when the letters are drawn into the image."""
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4), clip=clip, alpha=False)
    if pixmap.n < 3 or pixmap.width < 40 or pixmap.height < 40:
        return {}
    # Copy once. Reading pixmap.samples inside the loop copies the whole image every pixel.
    data = pixmap.samples
    channels = pixmap.n
    width = pixmap.width

    def bands(across: str) -> list[tuple[int, int]]:
        size = pixmap.height if across == "y" else pixmap.width
        other = pixmap.width if across == "y" else pixmap.height
        step = max(1, other // 70)
        found: list[tuple[int, int]] = []
        run: list[int] | None = None
        for index in range(size):
            white = 0
            seen = 0
            for cursor in range(0, other, step):
                seen += 1
                offset = ((index * width + cursor) if across == "y" else (cursor * width + index)) * channels
                if data[offset] > 242 and data[offset + 1] > 242 and data[offset + 2] > 242:
                    white += 1
            gutter = seen > 0 and white / seen > 0.96 and 0.08 * size < index < 0.92 * size
            if gutter:
                run = [index, index] if run is None else [run[0], index]
            elif run is not None:
                if run[1] - run[0] >= 4:
                    found.append((run[0], run[1]))
                run = None
        if run is not None and run[1] - run[0] >= 4:
            found.append((run[0], run[1]))
        return found

    def segments(size: int, gutters: list[tuple[int, int]]) -> list[tuple[int, int]]:
        cuts = [0]
        for start, end in gutters:
            cuts.append((start + end) // 2)
        cuts.append(size)
        spans = []
        for start, end in zip(cuts, cuts[1:]):
            if end - start > size * 0.12:
                spans.append((start, end))
        return spans

    rows = segments(pixmap.height, bands("y"))
    columns = segments(pixmap.width, bands("x"))
    if len(rows) * len(columns) < 2 or len(rows) * len(columns) > 12:
        return {}
    cells: dict[str, pymupdf.Rect] = {}
    scale_x = clip.width / pixmap.width
    scale_y = clip.height / pixmap.height
    index = 0
    for top, bottom in rows:
        for left, right in columns:
            letter = chr(ord("A") + index)
            cells[letter] = pymupdf.Rect(
                clip.x0 + left * scale_x,
                clip.y0 + top * scale_y,
                clip.x0 + right * scale_x,
                clip.y0 + bottom * scale_y,
            )
            index += 1
    return cells


def _panel_images(page: pymupdf.Page, clip: pymupdf.Rect, letters: set[str]) -> dict[str, tuple[bytes, float]]:
    """Crop only the panel letters the paper actually cites."""
    if not letters:
        return {}
    cells = _label_cells(page, clip)
    if any(letter not in cells for letter in letters):
        for letter, cell in _gutter_cells(page, clip).items():
            cells.setdefault(letter, cell)
    images: dict[str, tuple[bytes, float]] = {}
    for letter in letters:
        cell = cells.get(letter)
        if cell is None:
            continue
        png = _render_clip(page, cell)
        if png is None:
            continue
        images[letter] = (png, max(cell.width, 1) / max(cell.height, 1))
    return images


def _image_clip(page: pymupdf.Page, caption: pymupdf.Rect, column: pymupdf.Rect, top: float) -> pymupdf.Rect | None:
    found: list[pymupdf.Rect] = []
    for info in page.get_image_info():
        box = pymupdf.Rect(info["bbox"])
        if box.height < 15 or box.width < 15:
            continue
        if box.y1 > caption.y0 + 6 or box.y0 < top - 8:
            continue
        if box.x1 < column.x0 or box.x0 > column.x1:
            continue
        found.append(box)
    if not found:
        return None
    clip = found[0]
    for box in found[1:]:
        clip |= box
    return clip + (-2, -2, 2, 2)


def find_figures_and_refs(
    doc: pymupdf.Document,
    on_page: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, Figure], list[Reference]]:
    figures: dict[str, Figure] = {}
    page_lines: list[tuple[int, list[list[tuple]], list[pymupdf.Rect]]] = []
    page_total = max(doc.page_count, 1)
    for page_index, page in enumerate(doc):
        if on_page is not None:
            on_page(page_index, page_total)
        lines = _lines(page)
        words = [word for line in lines for word in line]
        caption_rects: list[pymupdf.Rect] = []
        for line in lines:
            kind = _label_kind(line[0][4])
            numbers = _numbers(line[1][4]) if kind and len(line) > 1 else []
            numbers = [(number, panel) for number, panel in numbers if number]
            if not kind or not numbers:
                continue
            caption = _union(line[:2])
            column = _column(caption, page.rect)
            above = [
                word[3]
                for word in words
                if word[3] < caption.y0 - 1
                and column.x0 - 10 <= (word[0] + word[2]) / 2 <= column.x1 + 10
                and not _panel_token(word[4])
            ]
            top = max(above) if above else page.rect.y0 + 28
            gap = caption.y0 - top
            image_clip = _image_clip(page, caption, column, top)
            if gap < 22 and image_clip is None:
                continue
            clip = image_clip or pymupdf.Rect(column.x0, top + 2, column.x1, caption.y0 - 2)
            png = _render_clip(page, clip)
            if png is None and image_clip is not None:
                png = _render_clip(page, image_clip)
                clip = image_clip
            if png is None:
                continue
            key = f"{kind}:{numbers[0][0]}"
            caption_rect = _caption_span(page, caption, column)
            with_caption = pymupdf.Rect(clip.x0, clip.y0, max(clip.x1, caption_rect.x1), caption_rect.y1)
            caption_png = _render_clip(page, with_caption) or png
            caption_rects.append(_union(line))
            if key not in figures:
                figures[key] = Figure(
                    key=key,
                    page=page_index,
                    clip=clip,
                    png=png,
                    aspect=max(clip.width, 1) / max(clip.height, 1),
                    caption_png=caption_png,
                    caption_aspect=max(with_caption.width, 1) / max(with_caption.height, 1),
                    panels={},
                )
        page_lines.append((page_index, lines, caption_rects))

    references: list[Reference] = []
    for page_index, lines, caption_rects in page_lines:
        for line in lines:
            line_rect = _union(line)
            if any(line_rect.intersects(caption) and abs(line_rect.y0 - caption.y0) < 2 for caption in caption_rects):
                continue
            index = 0
            while index < len(line):
                kind = _label_kind(line[index][4])
                if not kind or index + 1 >= len(line):
                    index += 1
                    continue
                number_index = index + 1
                parsed = [(number, panel) for number, panel in _numbers(line[number_index][4]) if number]
                if not parsed:
                    index += 1
                    continue
                pieces = [line[index], line[number_index]]
                extra: list[tuple] = []
                cursor = number_index + 1
                if parsed[0][1] == "" and cursor < len(line):
                    letter = _panel_token(line[cursor][4])
                    if letter:
                        parsed[0] = (parsed[0][0], letter)
                        pieces.append(line[cursor])
                        cursor += 1
                while cursor < len(line):
                    token = _clean_token(line[cursor][4]).lower()
                    if token in {"and", "&"} or token in {"-", "–", "—"}:
                        cursor += 1
                        continue
                    more = [(number, panel) for number, panel in _numbers(line[cursor][4]) if number]
                    if not more or cursor > number_index + 4:
                        break
                    extra.append(line[cursor])
                    parsed.extend(more)
                    cursor += 1
                first_rect = _union(pieces)
                rects = [first_rect]
                rects.extend(pymupdf.Rect(word[:4]) for word in extra)
                for rect, (number, panel) in zip(rects, parsed):
                    key = f"{kind}:{number}"
                    if key not in figures:
                        continue
                    label = "Figure" if kind == "figure" else "Scheme"
                    shown = f"{label} {number}{panel}"
                    references.append(Reference(page_index, rect + (-1, -1, 1, 1), key, shown, panel))
                index = max(cursor, number_index + 1)
    wanted: dict[str, set[str]] = {}
    for ref in references:
        if ref.panel:
            wanted.setdefault(ref.key, set()).add(ref.panel)
    for key, letters in wanted.items():
        figure = figures[key]
        figure.panels = _panel_images(doc[figure.page], figure.clip, letters)
    return figures, references


def _jpeg_xobject(doc: pymupdf.Document, png: bytes) -> int:
    pixmap = pymupdf.Pixmap(png)
    if pixmap.alpha:
        pixmap = pymupdf.Pixmap(pixmap, 0)
    if pixmap.colorspace is None or pixmap.n != 3:
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
    jpeg = pixmap.tobytes("jpg")
    xref = doc.get_new_xref()
    doc.update_object(
        xref,
        (
            f"<< /Type /XObject /Subtype /Image /Width {pixmap.width} "
            f"/Height {pixmap.height} /ColorSpace /DeviceRGB "
            f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>"
        ),
    )
    doc.update_stream(xref, jpeg, compress=False)
    doc.xref_set_key(xref, "Filter", "/DCTDecode")
    return xref


def _pdf_string(text: str) -> str:
    return "<" + text.encode("utf-8").hex() + ">"


_HEADER = 16
_FOOTER = 22
_NUDGE = 40


def _plain(text: str, limit: int) -> str:
    letters = []
    for char in text:
        letters.append(char if 32 <= ord(char) < 127 and char not in "()\\" else " ")
    compact = " ".join("".join(letters).split())
    if len(compact) <= limit:
        return compact or "Picture"
    return compact[: limit - 1].rstrip() + "."


def _card_size(page: pymupdf.Rect, aspect: float) -> tuple[float, float]:
    image_w = min(232, page.width * 0.42)
    image_h = image_w / max(aspect, 0.35)
    if image_h > 160:
        image_h = 160
        image_w = min(image_h * aspect, page.width * 0.42)
    return max(image_w, 196), image_h + _HEADER + _FOOTER


def _card_rect(
    anchor: pymupdf.Rect,
    page: pymupdf.Rect,
    aspect: float,
    avoid: list[pymupdf.Rect],
) -> pymupdf.Rect:
    """Open the card in the nearest spot that leaves the clicked word readable."""
    card_w, card_h = _card_size(page, aspect)
    gap = 8

    def place(x: float, y: float) -> pymupdf.Rect | None:
        if x < page.x0 + 4 or y < page.y0 + 4:
            return None
        if x + card_w > page.x1 - 4 or y + card_h > page.y1 - 4:
            return None
        return pymupdf.Rect(x, y, x + card_w, y + card_h)

    aligned = min(max(page.x0 + 8, anchor.x0), page.x1 - card_w - 8)
    beside_y = min(max(page.y0 + 8, anchor.y0 - _HEADER), page.y1 - card_h - 8)
    candidates = [
        place(anchor.x1 + gap, beside_y),
        place(aligned, anchor.y0 - gap - card_h),
        place(aligned, anchor.y1 + gap),
        place(page.x1 - card_w - 8, page.y1 - card_h - 8),
    ]
    fitted = [rect for rect in candidates if rect is not None]

    def score(rect: pymupdf.Rect) -> tuple[float, float, float]:
        own = (rect & anchor).get_area()
        others = sum((rect & box).get_area() for box in avoid)
        distance = abs(rect.x0 - anchor.x0) + abs(rect.y0 - anchor.y0)
        return (own > 1, distance, others)

    return min(fitted, key=score)


def _set_form_appearance(
    doc: pymupdf.Document,
    xref: int,
    rect: pymupdf.Rect,
    image_xref: int,
    label: str,
    aspect: float,
) -> None:
    width, height = rect.width, rect.height
    band_h = max(height - _HEADER, 12)
    draw_w = width - 10
    draw_h = draw_w / max(aspect, 0.2)
    if draw_h > band_h:
        draw_h = band_h
        draw_w = draw_h * aspect
    origin_x = (width - draw_w) / 2
    origin_y = (band_h - draw_h) / 2
    title = _plain(label, max(8, int((width - 14) / 5.1)))
    form = doc.get_new_xref()
    doc.update_object(
        form,
        (
            f"<< /Type /XObject /Subtype /Form /BBox [0 0 {width:.2f} {height:.2f}] "
            f"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> "
            f"/XObject << /Im0 {image_xref} 0 R >> >> >>"
        ),
    )
    stream = (
        f"q\n1 1 1 rg\n0 0 {width:.2f} {height:.2f} re f\n"
        f"0.93 0.95 0.93 rg\n0 {height - _HEADER:.2f} {width:.2f} {_HEADER:.2f} re f\nQ\n"
        f"q\n{draw_w:.2f} 0 0 {draw_h:.2f} {origin_x:.2f} {origin_y:.2f} cm\n/Im0 Do\nQ\n"
        f"q\n1.15 w\n0.05 0.42 0.32 RG\n0.7 0.7 {width - 1.4:.2f} {height - 1.4:.2f} re S\nQ\n"
        f"BT /F1 9 Tf 0.08 0.24 0.16 rg 6 {height - 12:.2f} Td ({title}) Tj ET\n"
    ).encode()
    doc.update_stream(form, stream)
    doc.xref_set_key(xref, "AP", f"<< /N {form} 0 R >>")


@dataclass
class Popup:
    page: int
    anchor: pymupdf.Rect
    png: bytes
    aspect: float
    kind: str
    label: str
    expanded_png: bytes | None = None
    panel: str = ""


def find_molecule_mentions(
    doc: pymupdf.Document,
    on_page: Callable[[int, int], None] | None = None,
    on_draw: Callable[[int, int], None] | None = None,
) -> list[Popup]:
    """Ligands, IUPAC names, and labels the paper itself defines, such as L1."""
    import chemistry

    prepared: list[tuple[int, str, list[tuple[int, int, tuple]], list]] = []
    learned: dict[str, chemistry.Structure] = {}
    page_total = max(doc.page_count, 1)
    for page_index, page in enumerate(doc):
        if on_page is not None:
            on_page(page_index, page_total)
        grouped: dict[int, list[list[tuple]]] = {}
        for line in _lines(page):
            if line:
                grouped.setdefault(line[0][5], []).append(line)
        for block in grouped.values():
            index = 0
            while index < len(block):
                line = list(block[index])
                continuation = block[index + 1] if index + 1 < len(block) else None
                joined = bool(line) and line[-1][4].endswith("-") and continuation is not None
                glue: set[int] = set()
                if joined and continuation:
                    last = line[-1][4]
                    if len(last) >= 2 and last[-2].isalpha():
                        last = last[:-1]
                        line[-1] = (*line[-1][:4], last, *line[-1][5:])
                    glue.add(id(continuation[0]))
                    line.extend(continuation)
                words = []
                placed: list[tuple[int, int, tuple]] = []
                cursor = 0
                for word in line:
                    text = chemistry.normalize_chemical(word[4])
                    if not text:
                        continue
                    if words and id(word) not in glue:
                        words.append(" ")
                        cursor += 1
                    start = cursor
                    words.append(text)
                    cursor += len(text)
                    placed.append((start, cursor, word))
                text = "".join(words)
                spans = chemistry.find_spans(text)
                prepared.append((page_index, text, placed, spans))
                learned.update(chemistry.aliases_near(text, spans))
                index += 2 if joined else 1
    pending: list[tuple[int, list[tuple], chemistry.Span]] = []
    smiles_order: list[str] = []
    seen: set[str] = set()
    for page_index, text, placed, spans in prepared:
        if learned:
            spans = chemistry.apply_aliases(text, spans, learned)
        for span in spans:
            if span.structure.kind not in {"molecule", "ligand"}:
                continue
            covered = [word for start, end, word in placed if start < span.end and end > span.start]
            if not covered:
                continue
            smiles = span.structure.smiles
            if smiles not in seen:
                seen.add(smiles)
                smiles_order.append(smiles)
            pending.append((page_index, covered, span))
    drawings: dict[str, bytes] = {}
    draw_total = len(smiles_order)
    for index, smiles in enumerate(smiles_order):
        if on_draw is not None:
            on_draw(index, draw_total)
        drawings[smiles] = chemistry.depict_png(smiles)
    mentions: list[Popup] = []
    for page_index, covered, span in pending:
        png = drawings.get(span.structure.smiles) or b""
        if not png:
            continue
        mentions.append(
            Popup(
                page=page_index,
                anchor=_union(covered) + (-0.4, -0.4, 0.4, 0.4),
                png=png,
                aspect=360 / 220,
                kind="molecule",
                label=span.surface,
            )
        )
    return mentions


_CONTROLS = (
    ("L", -_NUDGE, 0),
    ("R", _NUDGE, 0),
    ("U", 0, _NUDGE),
    ("D", 0, -_NUDGE),
    ("O", 0, 0),
    ("I", 0, 0),
    ("X", 0, 0),
)


def _mark(page: pymupdf.Page, rect: pymupdf.Rect, kind: str) -> pymupdf.Rect:
    if kind == "figure":
        fill, line = (1, 0.72, 0.02), (0.72, 0.38, 0.0)
    else:
        fill, line = (0.25, 0.78, 0.42), (0.02, 0.38, 0.18)
    padded = pymupdf.Rect(rect.x0 - 0.5, rect.y0 - 1.0, rect.x1 + 0.5, rect.y1 + 0.4)
    highlight = page.add_highlight_annot(padded)
    highlight.set_colors(stroke=fill)
    highlight.set_opacity(0.82)
    highlight.update()
    underline = page.add_underline_annot(rect)
    underline.set_colors(stroke=line)
    underline.update()
    return padded


def _control_rects(card: pymupdf.Rect) -> dict[str, pymupdf.Rect]:
    size = 16
    y0 = card.y1 - _FOOTER + (_FOOTER - size) / 2
    rects = {}
    x = card.x0 + 5
    for key, _dx, _dy in _CONTROLS:
        if key == "X":
            x = card.x1 - 5 - size
        rects[key] = pymupdf.Rect(x, y0, x + size, y0 + size)
        if key != "X":
            x += size + 3
    return rects


def _add_button(page: pymupdf.Page, name: str, rect: pymupdf.Rect, hidden: bool) -> None:
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_BUTTON
    widget.field_name = name
    widget.field_flags = pymupdf.PDF_BTN_FIELD_IS_PUSHBUTTON
    widget.rect = rect
    widget.border_width = 0
    widget.border_color = None
    if hidden:
        widget.fill_color = (1, 1, 1)
    annot = page.add_widget(widget)
    flags = 0 if not hidden else pymupdf.PDF_ANNOT_IS_HIDDEN
    if hidden:
        flags |= pymupdf.PDF_ANNOT_IS_PRINT
    annot.set_flags(flags)


def _parts(name: str) -> list[str]:
    return [name, *[name + key for key, *_rest in _CONTROLS]]


def _toggle_script(name: str) -> str:
    parts = _parts(name)
    listed = ",".join(f"'{part}'" for part in parts)
    return (
        f"var name='{name}';"
        f"var parts=[{listed}];"
        f"var suffix=[{','.join(chr(39) + key + chr(39) for key, *_rest in _CONTROLS)}];"
        "if(this.openPop && this.openPop!=name){"
        "var old=[this.openPop]; for(var s=0;s<suffix.length;s++) old.push(this.openPop+suffix[s]);"
        "for(var i=0;i<old.length;i++){var h=this.getField(old[i]); if(h) h.display=display.hidden;}}"
        "var shown=!this.getField(name).hidden;"
        "for(var j=0;j<parts.length;j++){var f=this.getField(parts[j]); if(f) f.display=shown?display.hidden:display.visible;}"
        "this.openPop=shown?'':name;"
    )


def _nudge_script(name: str, dx: int, dy: int) -> str:
    parts = _parts(name)
    listed = ",".join(f"'{part}'" for part in parts)
    return (
        f"var parts=[{listed}];"
        f"var dx={dx}; var dy={dy};"
        "var edge=this.getPageBox('Crop', this.pageNum);"
        "var minx=Math.min(edge[0],edge[2])+8; var maxx=Math.max(edge[0],edge[2])-8;"
        "var miny=Math.min(edge[1],edge[3])+8; var maxy=Math.max(edge[1],edge[3])-8;"
        f"var lead=this.getField('{name}');"
        "if(lead){var r=lead.rect;"
        "var left=Math.min(r[0],r[2]), right=Math.max(r[0],r[2]);"
        "var low=Math.min(r[1],r[3]), high=Math.max(r[1],r[3]);"
        "if(left+dx<minx) dx=minx-left; if(right+dx>maxx) dx=maxx-right;"
        "if(low+dy<miny) dy=miny-low; if(high+dy>maxy) dy=maxy-high;}"
        "for(var i=0;i<parts.length;i++){"
        "var f=this.getField(parts[i]); if(!f) continue; var box=f.rect;"
        "f.rect=[box[0]+dx, box[1]+dy, box[2]+dx, box[3]+dy];}"
    )


def _scale_script(name: str, factor: float) -> str:
    parts = _parts(name)
    listed = ",".join(f"'{part}'" for part in parts)
    return (
        f"var parts=[{listed}]; var factor={factor};"
        "var edge=this.getPageBox('Crop', this.pageNum);"
        "var minx=Math.min(edge[0],edge[2])+6; var maxx=Math.max(edge[0],edge[2])-6;"
        "var miny=Math.min(edge[1],edge[3])+6; var maxy=Math.max(edge[1],edge[3])-6;"
        "var left=1e9,right=-1e9,low=1e9,high=-1e9;"
        "for(var i=0;i<parts.length;i++){var f=this.getField(parts[i]); if(!f) continue; var b=f.rect;"
        "left=Math.min(left,b[0],b[2]); right=Math.max(right,b[0],b[2]);"
        "low=Math.min(low,b[1],b[3]); high=Math.max(high,b[1],b[3]);}"
        "var cx=(left+right)/2, cy=(low+high)/2;"
        "var nw=(right-left)*factor, nh=(high-low)*factor;"
        "if(nw<110||nh<80||nw>maxx-minx||nh>maxy-miny) return;"
        "for(var j=0;j<parts.length;j++){var g=this.getField(parts[j]); if(!g) continue; var r=g.rect;"
        "var l=Math.min(r[0],r[2]), rr=Math.max(r[0],r[2]);"
        "var btm=Math.min(r[1],r[3]), top=Math.max(r[1],r[3]);"
        "g.rect=[cx+(l-cx)*factor, cy+(top-cy)*factor, cx+(rr-cx)*factor, cy+(btm-cy)*factor];}"
    )


def _close_script(name: str) -> str:
    parts = _parts(name)
    listed = ",".join(f"'{part}'" for part in parts)
    return (
        f"var parts=[{listed}];"
        "for(var i=0;i<parts.length;i++){var f=this.getField(parts[i]); if(f) f.display=display.hidden;}"
        "this.openPop='';"
    )


def _set_action(doc: pymupdf.Document, xref: int, script: str) -> None:
    doc.xref_set_key(xref, "A", f"<< /S /JavaScript /JS {_pdf_string(script)} >>")


def _button_face(doc: pymupdf.Document, xref: int, rect: pymupdf.Rect, key: str) -> None:
    width, height = rect.width, rect.height
    if key == "L":
        icon = f"{width * 0.64:.2f} {height * 0.22:.2f} m {width * 0.64:.2f} {height * 0.78:.2f} l {width * 0.30:.2f} {height / 2:.2f} l h f"
    elif key == "R":
        icon = f"{width * 0.36:.2f} {height * 0.22:.2f} m {width * 0.36:.2f} {height * 0.78:.2f} l {width * 0.70:.2f} {height / 2:.2f} l h f"
    elif key == "U":
        icon = f"{width * 0.22:.2f} {height * 0.38:.2f} m {width * 0.78:.2f} {height * 0.38:.2f} l {width / 2:.2f} {height * 0.76:.2f} l h f"
    elif key == "D":
        icon = f"{width * 0.22:.2f} {height * 0.62:.2f} m {width * 0.78:.2f} {height * 0.62:.2f} l {width / 2:.2f} {height * 0.24:.2f} l h f"
    elif key == "I":
        icon = (
            f"{width * 0.22:.2f} {height * 0.42:.2f} {width * 0.56:.2f} {height * 0.16:.2f} re f "
            f"{width * 0.42:.2f} {height * 0.22:.2f} {width * 0.16:.2f} {height * 0.56:.2f} re f"
        )
    elif key == "O":
        icon = f"{width * 0.22:.2f} {height * 0.42:.2f} {width * 0.56:.2f} {height * 0.16:.2f} re f"
    else:
        icon = (
            f"{width * 0.30:.2f} {height * 0.30:.2f} m {width * 0.70:.2f} {height * 0.70:.2f} l S "
            f"{width * 0.70:.2f} {height * 0.30:.2f} m {width * 0.30:.2f} {height * 0.70:.2f} l S"
        )
    form = doc.get_new_xref()
    doc.update_object(
        form,
        f"<< /Type /XObject /Subtype /Form /BBox [0 0 {width:.2f} {height:.2f}] >>",
    )
    background = "0.98 0.92 0.90" if key == "X" else "0.88 0.95 0.92"
    ink = "0.55 0.14 0.10" if key == "X" else "0.04 0.32 0.22"
    color_op = "RG" if key == "X" else "rg"
    weight = "1.3 w " if key == "X" else ""
    stream = (
        f"q\n{background} rg\n0 0 {width:.2f} {height:.2f} re f\n"
        f"0.7 w\n0.05 0.42 0.32 RG\n0.45 0.45 {width - 0.9:.2f} {height - 0.9:.2f} re S\n"
        f"{weight}{ink} {color_op}\n{icon}\nQ\n"
    ).encode()
    doc.update_stream(form, stream)
    doc.xref_set_key(xref, "AP", f"<< /N {form} 0 R >>")


def _clear_face(doc: pymupdf.Document, xref: int, rect: pymupdf.Rect) -> None:
    width, height = rect.width, rect.height
    form = doc.get_new_xref()
    doc.update_object(
        form,
        f"<< /Type /XObject /Subtype /Form /BBox [0 0 {width:.2f} {height:.2f}] >>",
    )
    doc.update_stream(form, b"q\nQ\n")
    doc.xref_set_key(xref, "AP", f"<< /N {form} 0 R >>")


def _place_popup(doc: pymupdf.Document, popup: Popup, index: int, avoid: list[pymupdf.Rect]) -> None:
    page = doc[popup.page]
    name = f"p{index}"
    target = _mark(page, popup.anchor, popup.kind)
    _add_button(page, f"hit{index}", target, hidden=False)
    card = _card_rect(popup.anchor, page.rect, popup.aspect, avoid)
    picture = pymupdf.Rect(card.x0, card.y0, card.x1, card.y1 - _FOOTER - 2)
    _add_button(page, name, picture, hidden=True)
    buttons = _control_rects(card)
    for key, _dx, _dy in _CONTROLS:
        _add_button(page, name + key, buttons[key], hidden=True)


def _finish_popup(doc: pymupdf.Document, popup: Popup, index: int, image_xref: int) -> None:
    page = doc[popup.page]
    name = f"p{index}"
    widgets = {item.field_name: item for item in page.widgets()}
    image = widgets[name]
    _set_form_appearance(doc, image.xref, image.rect, image_xref, popup.label, popup.aspect)
    for key, dx, dy in _CONTROLS:
        widget = widgets[name + key]
        _button_face(doc, widget.xref, widget.rect, key)
        if key == "X":
            script = _close_script(name)
        elif key == "I":
            script = _scale_script(name, 1.25)
        elif key == "O":
            script = _scale_script(name, 0.8)
        else:
            script = _nudge_script(name, dx, dy)
        _set_action(doc, widget.xref, script)
    hit = widgets[f"hit{index}"]
    _clear_face(doc, hit.xref, hit.rect)
    doc.xref_set_key(hit.xref, "BS", "<< /W 0 /S /S >>")
    doc.xref_set_key(hit.xref, "MK", "<< >>")
    _set_action(doc, hit.xref, _toggle_script(name))


def prepare_pdf(source: bytes, progress: Progress | None = None) -> tuple[dict, dict, list[Popup]]:
    """Find the pictures. This does not build the downloadable PDF."""

    def report(label: str, fraction: float) -> None:
        if progress is not None:
            progress(label, fraction)

    report("Opening the PDF", 0.02)
    doc = pymupdf.open(stream=source, filetype="pdf")
    try:
        if doc.is_encrypted:
            raise ValueError("This PDF is encrypted. Open it without a password and try again.")
        page_sizes = [(float(page.rect.width), float(page.rect.height)) for page in doc]

        def figure_page(index: int, total: int) -> None:
            report(f"Finding figures, page {index + 1} of {total}", 0.04 + 0.50 * (index / total))

        figures, references = find_figures_and_refs(doc, figure_page)
        figure_word = "figure" if len(figures) == 1 else "figures"
        report(f"Found {len(figures)} {figure_word}", 0.56)
        popups: list[Popup] = []
        for ref in references:
            figure = figures[ref.key]
            if ref.panel and ref.panel in figure.panels:
                png, aspect = figure.panels[ref.panel]
            else:
                png, aspect = figure.png, figure.aspect
            popups.append(
                Popup(
                    page=ref.page,
                    anchor=ref.rect,
                    png=png,
                    aspect=aspect,
                    kind="figure",
                    label=ref.label,
                    expanded_png=figure.caption_png,
                    panel=ref.panel,
                )
            )
        occupied = [(popup.page, pymupdf.Rect(popup.anchor)) for popup in popups]

        def name_page(index: int, total: int) -> None:
            report(f"Reading chemical names, page {index + 1} of {total}", 0.58 + 0.28 * (index / total))

        def draw_step(index: int, total: int) -> None:
            report(f"Drawing structures, {index + 1} of {total}", 0.88 + 0.08 * (index / max(total, 1)))

        for mention in find_molecule_mentions(doc, name_page, draw_step):
            if any(mention.page == page and (mention.anchor & box).get_area() > 1 for page, box in occupied):
                continue
            popups.append(mention)
            occupied.append((mention.page, pymupdf.Rect(mention.anchor)))
        report("Opening the paper", 0.99)
        image_order: list[bytes] = []
        image_index: dict[bytes, int] = {}
        for popup in popups:
            for blob in (popup.png, popup.expanded_png):
                if blob and blob not in image_index:
                    image_index[blob] = len(image_order)
                    image_order.append(blob)
        summary = {
            "figures": len(figures),
            "references": len(references),
            "molecules": sum(1 for popup in popups if popup.kind == "molecule"),
            "keys": sorted(figures),
        }
        details = {
            "pages": page_sizes,
            "images": image_order,
            "popups": [
                {
                    "page": popup.page,
                    "anchor": [round(popup.anchor.x0, 2), round(popup.anchor.y0, 2), round(popup.anchor.x1, 2), round(popup.anchor.y1, 2)],
                    "label": popup.label,
                    "kind": popup.kind,
                    "image": image_index[popup.png],
                    "panel": popup.panel,
                    "expanded": None if popup.expanded_png is None else image_index[popup.expanded_png],
                }
                for popup in popups
            ],
        }
        return summary, details, popups
    finally:
        _drop_lines(doc)
        doc.close()


def embed_pdf(source: bytes, popups: list[Popup]) -> bytes:
    """Acrobat copy: click a highlight to open the picture. Built when the user downloads."""
    doc = pymupdf.open(stream=source, filetype="pdf")
    on_page: dict[int, list[pymupdf.Rect]] = {}
    for popup in popups:
        on_page.setdefault(popup.page, []).append(popup.anchor)
    for index, popup in enumerate(popups):
        _place_popup(doc, popup, index, on_page[popup.page])
    staged = doc.tobytes(deflate=True, garbage=1)
    doc.close()
    doc = pymupdf.open(stream=staged, filetype="pdf")
    images: dict[bytes, int] = {}
    for index, popup in enumerate(popups):
        if popup.png not in images:
            images[popup.png] = _jpeg_xobject(doc, popup.png)
        _finish_popup(doc, popup, index, images[popup.png])
    output = doc.tobytes(deflate=True, garbage=1)
    doc.close()
    return output


def annotate_pdf(source: bytes, progress: Progress | None = None) -> tuple[bytes, dict, dict]:
    summary, details, popups = prepare_pdf(source, progress)
    return embed_pdf(source, popups), summary, details


def _sample_pdf() -> bytes:
    doc = pymupdf.open()
    intro = doc.new_page()
    intro.insert_text(
        (72, 80),
        "The ligand is shown in Figure 1. Scheme 2 is the coupling.",
        fontsize=12,
    )
    intro.insert_text((72, 110), "See also Fig. 1a and Figures 1 and nothing else.", fontsize=12)
    intro.insert_text((72, 140), "The catalyst is triphenylphosphine.", fontsize=12)
    intro.insert_text((72, 170), "Ligand L1 (triphenylphosphine) and 4-methoxyaniline.", fontsize=12)
    figure = doc.new_page()
    shape = figure.new_shape()
    shape.draw_rect(pymupdf.Rect(72, 108, 188, 220))
    shape.finish(color=(0.05, 0.42, 0.32), fill=(0.86, 0.93, 0.9), width=2)
    shape.draw_circle((130, 164), 28)
    shape.finish(color=(0.55, 0.15, 0.1), fill=(0.85, 0.35, 0.25))
    shape.draw_rect(pymupdf.Rect(214, 108, 340, 220))
    shape.finish(color=(0.1, 0.2, 0.45), fill=(0.9, 0.92, 0.97), width=2)
    shape.commit()
    figure.insert_text((78, 100), "(a)", fontsize=11)
    figure.insert_text((220, 100), "(b)", fontsize=11)
    figure.insert_text((72, 252), "Figure 1. Test ligand.", fontsize=12)
    scheme = doc.new_page()
    shape = scheme.new_shape()
    shape.draw_rect(pymupdf.Rect(72, 80, 420, 200))
    shape.finish(color=(0.1, 0.2, 0.45), fill=(0.9, 0.92, 0.97), width=2)
    shape.commit()
    scheme.insert_text((72, 224), "Scheme 2. The coupling.", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def _appearance_has_figure(doc: pymupdf.Document, widget_xref: int) -> bool:
    appearance = doc.xref_get_key(widget_xref, "AP")[1]
    form = int(re.search(r"/N (\d+)", appearance).group(1))
    if b"/Im0 Do" not in doc.xref_stream(form):
        return False
    resources = doc.xref_get_key(form, "Resources")[1]
    image = int(re.search(r"/Im0 (\d+)", resources).group(1))
    pixmap = pymupdf.Pixmap(doc, image)
    if pixmap.n > 3:
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
    data = pixmap.samples
    colored = 0
    for offset in range(0, len(data), pixmap.n):
        red, green, blue = data[offset], data[offset + 1], data[offset + 2]
        if abs(red - green) > 18 or abs(green - blue) > 18 or red < 210:
            colored += 1
    return colored > 30


def self_test() -> dict:
    seen: list[tuple[str, float]] = []

    def progress(label: str, fraction: float) -> None:
        seen.append((label, fraction))

    output, summary, details = annotate_pdf(_sample_pdf(), progress)
    if not seen or seen[-1][1] < 0.98:
        raise RuntimeError(f"progress did not finish: {seen!r}")
    if any(seen[index][1] > seen[index + 1][1] + 1e-6 for index in range(len(seen) - 1)):
        raise RuntimeError(f"progress went backwards: {seen!r}")
    stages = " ".join(label for label, _fraction in seen)
    if "Finding figures" not in stages or "chemical names" not in stages or "Drawing structures" not in stages:
        raise RuntimeError(f"progress stages missing: {stages}")
    doc = pymupdf.open(stream=output, filetype="pdf")
    links = 0
    hidden = 0
    scripts = 0
    pictures = 0
    highlights = 0
    covered = 0
    card_text: list[bytes] = []
    hits: dict[str, pymupdf.Rect] = {}
    cards: dict[str, pymupdf.Rect] = {}
    blocked = 0
    buttons: dict[str, list[pymupdf.Rect]] = {}
    for page in doc:
        links += len(page.get_links())
        for annot in page.annots() or []:
            if annot.type[0] == pymupdf.PDF_ANNOT_HIGHLIGHT:
                highlights += 1
        for widget in page.widgets() or []:
            keys = doc.xref_get_keys(widget.xref)
            if "A" in keys and "JavaScript" in doc.xref_get_key(widget.xref, "A")[1]:
                scripts += 1
            flags = int(doc.xref_get_key(widget.xref, "F")[1] or "0")
            if flags & pymupdf.PDF_ANNOT_IS_HIDDEN:
                hidden += 1
            name = widget.field_name
            if name.startswith("hit") and name[3:].isdigit():
                hits[name[3:]] = widget.rect
            button = re.fullmatch(r"p(\d+)([A-Z]+)", name)
            if button:
                buttons.setdefault(button.group(1), []).append(widget.rect)
            if _appearance_has_figure(doc, widget.xref):
                pictures += 1
                if name.startswith("p") and name[1:].isdigit():
                    cards[name[1:]] = widget.rect
                form = int(re.search(r"/N (\d+)", doc.xref_get_key(widget.xref, "AP")[1]).group(1))
                card_text.append(doc.xref_stream(form))
    for key, rect in cards.items():
        if key in hits and (rect & hits[key]).get_area() > 2:
            covered += 1
        for button_rect in buttons.get(key, []):
            if (rect & button_rect).get_area() > 0.5:
                blocked += 1
    doc.close()
    popups = summary["references"] + summary["molecules"]
    pieces = 1 + len(_CONTROLS)
    titles = b"\n".join(card_text)
    if summary["figures"] != 2 or summary["references"] < 3 or summary["molecules"] < 1:
        raise RuntimeError(f"detection failed: {summary}")
    if len(details["popups"]) != popups or not details["images"]:
        raise RuntimeError("reader payload is missing pictures")
    if links != 0 or highlights < popups or hidden < popups * pieces or scripts < popups * pieces or pictures < popups:
        raise RuntimeError(
            f"annotations failed links={links} highlights={highlights} "
            f"hidden={hidden} scripts={scripts} pictures={pictures} popups={popups}"
        )
    if covered:
        raise RuntimeError(f"a card covers the highlighted word ({covered})")
    if blocked:
        raise RuntimeError(f"a move button sits under the picture ({blocked})")
    if b"Figure 1" not in titles or b"triphenylphosphine" not in titles:
        raise RuntimeError("a card is missing its title")
    panel = next((item for item in details["popups"] if item.get("panel") == "A"), None)
    if panel is None or panel.get("expanded") is None:
        raise RuntimeError(f"panel crop missing: {details['popups']}")
    panel_image = pymupdf.Pixmap(details["images"][panel["image"]])
    whole_image = pymupdf.Pixmap(details["images"][panel["expanded"]])
    if panel_image.width >= whole_image.width * 0.85:
        raise RuntimeError(
            f"panel is not cropped: panel={panel_image.width} whole={whole_image.width}"
        )
    labels = {item["label"] for item in details["popups"]}
    if "L1" not in labels or not any("methoxyaniline" in item["label"].lower() for item in details["popups"]):
        raise RuntimeError(f"names missed: {labels}")
    summary["bytes"] = len(output)
    return summary


if __name__ == "__main__":
    print(self_test())
