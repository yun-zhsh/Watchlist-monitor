"""Read the text of PDFs whose Chinese fonts carry no ToUnicode map.

Some reports (Bank of Nanjing's annual reports from 2017 on) embed CID-keyed Chinese fonts with the Identity-H encoding
and no ToUnicode map. pypdf then returns each character code (the CID) as if it were a Unicode code point, which gives
meaningless text. When such a font declares a standard Adobe character collection (Adobe-GB1 for simplified Chinese,
Adobe-CNS1 for traditional), the CID identifies the character, and Adobe's published CID-to-Unicode tables (in cmaps/,
see cmaps/NOTICE) recover the text.

add_missing_unicode_maps gives each such font an in-memory ToUnicode map before the text is extracted. Only fonts with
no ToUnicode map and an Adobe-GB1 or Adobe-CNS1 collection are touched; every other font decodes exactly as before.
The map lists only the CIDs in the font's CIDSet (the subset it embeds), because pypdf re-reads the map of a font on
every page and the full tables have about 30,000 entries. The file on disk is never changed.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import DecodedStreamObject, NameObject

CMAP_DIR = Path(__file__).parent / "cmaps"
TABLE_FILES = {"GB1": "Adobe-GB1-UCS2", "CNS1": "Adobe-CNS1-UCS2"}
CHUNK = 100  # a CMap bfchar block holds at most 100 entries


@lru_cache(maxsize=None)
def cid_to_unicode(ordering: str) -> dict[int, str]:
    """CID -> the UTF-16BE hex of its Unicode text, read from Adobe's table for the collection."""
    text = (CMAP_DIR / TABLE_FILES[ordering]).read_text(encoding="ascii")
    table: dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for cid, uni in re.findall(r"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>", block):
            table[int(cid, 16)] = uni.upper()
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for first, last, uni in re.findall(r"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>", block):
            for cid in range(int(first, 16), int(last, 16) + 1):
                table[cid] = f"{int(uni, 16) + cid - int(first, 16):04X}"
    return table


def font_cids(descriptor) -> list[int] | None:
    """The CIDs a subset font embeds, from the CIDSet bitmap of its font descriptor (None when there is none)."""
    cid_set = descriptor.get("/CIDSet")
    if cid_set is None:
        return None
    data = cid_set.get_object().get_data()
    return [i * 8 + bit for i, byte in enumerate(data) for bit in range(8) if byte & (0x80 >> bit)]


def unicode_map(table: dict[int, str], cids: list[int] | None) -> DecodedStreamObject:
    entries = [(cid, table[cid]) for cid in (sorted(table) if cids is None else cids) if cid in table]
    lines = ["/CIDInit /ProcSet findresource begin 12 dict begin begincmap", "/CMapType 2 def", "1 begincodespacerange <0000> <FFFF> endcodespacerange"]
    for start in range(0, len(entries), CHUNK):
        chunk = entries[start : start + CHUNK]
        lines += [f"{len(chunk)} beginbfchar", *(f"<{cid:04X}> <{uni}>" for cid, uni in chunk), "endbfchar"]
    lines.append("endcmap CMapName currentdict /CMap defineresource pop end end")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(lines).encode("ascii"))
    return stream


def collection(font) -> str | None:
    """"GB1" or "CNS1" for an Identity-H CID font without a ToUnicode map that uses that Adobe collection; otherwise None."""
    if font.get("/Subtype") != "/Type0" or font.get("/Encoding") != "/Identity-H" or "/ToUnicode" in font:
        return None
    descendants = font.get("/DescendantFonts")
    if not descendants:
        return None
    info = descendants[0].get_object().get("/CIDSystemInfo")
    if info is None:
        return None
    info = info.get_object()
    ordering = str(info.get("/Ordering"))
    return ordering if str(info.get("/Registry")) == "Adobe" and ordering in TABLE_FILES else None


def add_missing_unicode_maps(reader: PdfReader) -> int:
    """Give every Adobe-GB1/CNS1 font of the document that lacks a ToUnicode map one built from Adobe's table (in memory).

    Returns the number of fonts changed. Call it before extract_text; do not write the pages of this reader to a file.
    """
    changed = 0
    seen: set[int] = set()

    def visit(resources) -> None:
        nonlocal changed
        if resources is None:
            return
        resources = resources.get_object()
        if id(resources) in seen:
            return
        seen.add(id(resources))
        fonts = resources.get("/Font")
        for ref in (fonts.get_object().values() if fonts is not None else []):
            font = ref.get_object()
            ordering = collection(font)
            if ordering is None:
                continue
            descendant = font["/DescendantFonts"][0].get_object()
            cids = font_cids(descendant["/FontDescriptor"].get_object())
            font[NameObject("/ToUnicode")] = unicode_map(cid_to_unicode(ordering), cids)
            changed += 1
        xobjects = resources.get("/XObject")
        for ref in (xobjects.get_object().values() if xobjects is not None else []):
            xobject = ref.get_object()
            if xobject.get("/Subtype") == "/Form":
                visit(xobject.get("/Resources"))  # text drawn inside a form uses the form's own fonts

    for page in reader.pages:
        visit(page.get("/Resources"))
    return changed
