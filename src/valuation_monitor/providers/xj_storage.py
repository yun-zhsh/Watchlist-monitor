"""Xj-Storage (xj-storage.jp), the host of the IR library of many Japanese listed companies: the original Japanese Kessan Tanshin
(summaries of financial results) as filed on TDnet, the Tokyo Stock Exchange's timely disclosure system.

The company's own IR website lists its library from Xj-Storage's public list (GetList.aspx, JSON), which gives the title, the
disclosure category, the publication time and the PDF of every document. TDnet itself keeps filings for only about a month and
EDINET's API needs a key, so this is where the full history is.

Downloaded files are saved in data/filings/<symbol>/ and index.json there records the English title, publication time and source
link of every filing. To keep the repository small, only the summary pages the parser reads are kept (the first two or three pages;
for a correction its cover letter as well), unchanged.

Publication time: the time the list gives (Tokyo time), which is the TDnet disclosure time (a Kessan Tanshin is usually published at
15:00, or 15:30 since the close moved to 15:30 in November 2024).

Corrections: a correction of a Kessan Tanshin reprints it in full with the corrected figures. Its figures are used from the
correction's own publication time on; until then the original's are (see filings.build_history). A correction whose text has no
corrected summary (SHIFT's November 2021 correction of the cash flows only) changes nothing the program reads: it is recorded in
the index without a file.

The Japanese-language patterns that recognise the titles are in jp_patterns.py.
"""

from __future__ import annotations

import io
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
from pypdf import PdfReader, PdfWriter

from valuation_monitor import jp_patterns as jp
from valuation_monitor.jp_tanshin import english_title, has_summary, page_texts, summary_pages
from valuation_monitor.providers.base import Filing, Release, save_index
from valuation_monitor.providers.http import HttpClient

LIST_URL = "https://www.xj-storage.jp/public-list/GetList.aspx"
COMPANIES = {"3697.T": "AS95685"}  # Yahoo symbol -> Xj-Storage company code (from the IR library page of the company's website)
# Disclosure categories of a Kessan Tanshin: full year, second quarter, first quarter, third quarter, other quarters, numerical data correction
TANSHIN_CATEGORIES = ("3", "4", "16", "18", "20", "25")
PREFERRED_CORRECTION_CATEGORIES = ("25", "20")  # one correction can be listed twice; the numerical data correction is taken
LISTING_BASIS = "Xj-Storage listing time (the TDnet disclosure time)"
NO_SUMMARY_NOTE = "corrects only figures the program does not read (no corrected summary of results); not saved"


def classify(title: str) -> tuple[int, int, int, bool] | None:
    """A Japanese library title -> (fiscal year, fiscal quarter (4 = full year), month the fiscal year ends, correction); None if not a Kessan Tanshin."""
    title = unicodedata.normalize("NFKC", title)
    if jp.TANSHIN_WORD not in title or any(word in title for word in jp.EXCLUDED_TITLE_WORDS):
        return None
    m = re.search(jp.TITLE_RE, title)
    if m is None:
        return None
    year = int(m.group("year")) if m.group("year") else int(m.group("heisei")) + jp.HEISEI_OFFSET
    return year, int(m.group("quarter") or 4), int(m.group("month")), jp.CORRECTION_WORD in title


def period_end(fiscal_year: int, quarter: int, year_end_month: int) -> pd.Timestamp:
    """The last day of a fiscal quarter (quarter 4: the fiscal year end)."""
    end = pd.Period(year=fiscal_year, month=year_end_month, freq="M") - 3 * (4 - quarter)
    return end.end_time.normalize()


def listing_time(text: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.strptime(text, "%Y/%m/%d %H:%M:%S"))


def excerpt_pages(pdf: bytes) -> bytes:
    """Keep only the pages up to the end of the summary (see jp_tanshin.summary_pages); the pages are unchanged."""
    reader = PdfReader(io.BytesIO(pdf))
    keep = summary_pages(page_texts(pdf))  # read from a separate copy: the pages written below stay unchanged
    if len(keep) == len(reader.pages):
        return pdf
    writer = PdfWriter()
    for i in keep:
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class XjStorageProvider:
    def __init__(self, http: HttpClient | None, archive_dir: str | Path = "data/filings"):
        self.http = http
        self.archive_dir = Path(archive_dir)

    def release_info(self, symbol: str) -> dict[str, Release]:
        """No figures come from a document published after its record (a correction is a record of its own): always empty."""
        return {}

    def results_announcements(self, symbol: str, since: datetime) -> list[Filing]:
        """The Kessan Tanshin and their corrections published since `since`, oldest first."""
        folder = self.archive_dir / symbol.upper()
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        try:
            added = self._fetch_new(symbol, since, folder, index)
        except Exception as e:
            if not index:
                raise
            print(f"{symbol}: cannot reach Xj-Storage ({e}); using the {len(index)} saved filings", file=sys.stderr)
            added = False
        if added:
            index.sort(key=lambda e: (e["published"], e["kind"], e["url"]))
            save_index(index_path, index)

        since_ts = pd.Timestamp(since)
        filings = []
        for e in index:
            if not e.get("file") or pd.Timestamp(e["published"]) < since_ts:
                continue
            note = ""
            if e["kind"] == "correction":
                original = next(
                    (x for x in index if x["kind"] == "tanshin" and (x["fiscal_year"], x["quarter"]) == (e["fiscal_year"], e["quarter"])), None
                )
                if original is not None:
                    note = (
                        f"Correction of the Kessan Tanshin published {pd.Timestamp(original['published']):%Y-%m-%d %H:%M}: its corrected figures "
                        "are used from this publication on, the original ones before"
                    )
            filings.append(
                Filing(
                    title=e["title"],
                    published=pd.Timestamp(e["published"]),
                    url=e["url"],
                    content=(folder / e["file"]).read_bytes(),
                    note=note,
                    correction=e["kind"] == "correction",
                )
            )
        return filings

    def _listing(self, company: str) -> list[dict]:
        params = {"company": company, "len": "10000", "doctype": ",".join(TANSHIN_CATEGORIES), "filetype": "PDF-GENERAL", "output": "json"}
        return json.loads(self.http.get_bytes(LIST_URL, params).decode("utf-8-sig")).get("items") or []

    def _fetch_new(self, symbol: str, since: datetime, folder: Path, index: list[dict]) -> bool:
        company = COMPANIES.get(symbol.upper())
        if company is None:
            raise ValueError(f"{symbol}: no Xj-Storage company code is known (add it to COMPANIES in providers/xj_storage.py)")
        known = {e["url"] for e in index}
        candidates: dict[tuple, list[tuple[pd.Timestamp, int, dict, tuple]]] = {}
        for item in self._listing(company):
            if item.get("disclosureCode") not in TANSHIN_CATEGORIES or not item.get("files"):
                continue
            cls = classify(item["title"])
            if cls is None:
                continue
            published = listing_time(item["publishDate"])
            if published < pd.Timestamp(since) - pd.DateOffset(years=1):  # one more year: the year-to-date figures need the earlier quarters
                continue
            fiscal_year, quarter, _, correction = cls
            # one filing per period (and one per correction time): the same document is sometimes listed under two categories
            key = (fiscal_year, quarter, "correction", published) if correction else (fiscal_year, quarter, "tanshin")
            rank = 0 if correction and item["disclosureCode"] in PREFERRED_CORRECTION_CATEGORIES else 1
            candidates.setdefault(key, []).append((published, rank, item, cls))

        added = False
        for key, items in sorted(candidates.items(), key=lambda kv: min(i[0] for i in kv[1])):
            published, _, item, (fiscal_year, quarter, year_end_month, correction) = min(items, key=lambda i: (i[0], i[1]))
            url = item["files"][0]["url"]
            if url in known or any(i[2]["files"][0]["url"] in known for i in items):
                continue
            if not correction and any((e["kind"], e["fiscal_year"], e["quarter"]) == ("tanshin", fiscal_year, quarter) for e in index):
                continue
            end = period_end(fiscal_year, quarter, year_end_month)
            entry = {
                "kind": "correction" if correction else "tanshin",
                "fiscal_year": fiscal_year,
                "quarter": quarter,
                "period_end": f"{end:%Y-%m-%d}",
                "title": english_title(fiscal_year, quarter, end, correction),
                "published": f"{published:%Y-%m-%dT%H:%M:%S}",
                "basis": LISTING_BASIS,
                "url": url,
                "file": None,
            }
            pdf = self.http.get_bytes(url, cache=False)
            if correction and not has_summary(" ".join(page_texts(pdf))):
                entry["note"] = NO_SUMMARY_NOTE
                print(f"{symbol}: {entry['title']} ({published}) {NO_SUMMARY_NOTE}", file=sys.stderr)
            else:
                folder.mkdir(parents=True, exist_ok=True)
                entry["file"] = f"{published:%Y-%m-%d}_{url.rsplit('/', 1)[-1]}"
                (folder / entry["file"]).write_bytes(excerpt_pages(pdf))
                print(f"{symbol}: new filing {entry['title']}, published {published} ({LISTING_BASIS})", file=sys.stderr)
            index.append(entry)
            known.add(url)
            added = True
        return added
