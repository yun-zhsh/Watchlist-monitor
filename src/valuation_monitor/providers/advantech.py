"""Advantech IR website (advantech.com/en/investor): English consolidated financial statements (Taiwan-IFRS, reviewed or audited by the accountants).

The Taiwan disclosure platforms (MOPS and its document server) refuse requests from cloud networks, so the company's own IR website is used. Three kinds of PDF:
  - quarterly   Quarterly/<year>_Q<n>_Consolidated-English.pdf (2013 uses <year>_<n>Q_...): the first three quarters of each year
  - annual      Annual/<year>_Annual_Consolidated_English.pdf: the audited full-year statements, on the website from 2018 on
  - annual_report  Shareholder-Annual-Report/<ROC year>/1-en.pdf: the English shareholder annual report (ROC year 106 = 2017, covering 2016),
                which contains the audited consolidated statements; used only for the years without an annual statements file (2015-2017)
Only the pages the parser reads are saved (tifrs.wanted_pages: about 8-12 of 75-325 pages), in data/filings/<symbol>/ from FIRST_YEAR on: the annual statements before 2015 are on neither page, so no fourth quarter before 2015 could be built.

Publication time. The PDFs carry no reliable upload time (the file dates on the website were reset in 2025), but every set of statements states the day the
board of directors approved it, and the company publishes its results that day (on MOPS and in its press release, e.g. the 3Q26 release is dated
2026-10-29, the day the 3Q26 statements were approved). The time of day is not given, so the figures are taken as published after the close of the Taiwan
Stock Exchange (13:30): the first closing price that may reflect them is the next trading day's. The FY2015-FY2017 figures come from the shareholder annual
report, published a few months later, but they were public from the day the statements were approved, so that day is kept as well (see release_info).
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from pypdf import PdfReader, PdfWriter

from valuation_monitor.providers.base import Filing, Release, save_index
from valuation_monitor.providers.http import HttpClient
from valuation_monitor.tifrs import approval, page_texts, wanted_pages

PAGES = ["https://www.advantech.com/en/investor/quarterly-results", "https://www.advantech.com/en/investor/annual-report"]
QUARTERLY_RE = re.compile(r"https://advcloudfiles\.advantech\.com/investor/Quarterly/(?P<year>20\d\d)_(?:Q(?P<q>[1-3])|(?P<q2>[1-3])Q)_Consolidated-English\.pdf")
ANNUAL_RE = re.compile(r"https://advcloudfiles\.advantech\.com/investor/Annual/(?P<year>20\d\d)_Annual_Consolidated_English\.pdf")
ANNUAL_REPORT_RE = re.compile(r"https://advcloudfiles\.advantech\.com/investor/Shareholder-Annual-Report/(?P<roc>1\d\d)/1-en\.pdf")
ROC_OFFSET = 1911  # ROC year + 1911 = Gregorian year
FIRST_YEAR = 2015  # the first year saved: its quarterly reports carry the 2014 figures as comparatives; no annual statements before 2015 are on the website
PUBLISHED_TIME = "13:30"  # only the day is stated; at the close, so the next trading day is the first that may reflect the figures
PUBLISHED_NOTE = "the statements give only the day the board of directors approved them (the day the company published its results); taken as after the 13:30 close, so the figures count from the next trading day"
ANNUAL_REPORT_NOTE = (
    "the full-year figures are read from the English shareholder annual report, published a few months after the statements were approved; "
    "they were public from the approval day, which is kept as the publication time"
)
KIND_TITLES = {"quarterly": "consolidated financial statements", "annual": "consolidated financial statements", "annual_report": "shareholder annual report"}


def excerpt(pdf: bytes, pages: list[int]) -> bytes:
    """Keep only the given pages of the report (unchanged), as is done for the A-share reports: the full reports are 75-325 pages."""
    reader = PdfReader(io.BytesIO(pdf))
    writer = PdfWriter()
    for i in pages:
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def discover(html_pages: list[str]) -> dict[tuple[int, int], tuple[str, str]]:
    """The statement files on the pages: {(year, quarter): (kind, link)}; the fourth quarter is the full year.

    The annual statements file of a year is preferred to the shareholder annual report of that year.
    """
    found: dict[tuple[int, int], tuple[str, str]] = {}
    reports: dict[tuple[int, int], tuple[str, str]] = {}
    for html in html_pages:
        for m in QUARTERLY_RE.finditer(html):
            found[(int(m.group("year")), int(m.group("q") or m.group("q2")))] = ("quarterly", m.group(0))
        for m in ANNUAL_RE.finditer(html):
            found[(int(m.group("year")), 4)] = ("annual", m.group(0))
        for m in ANNUAL_REPORT_RE.finditer(html):
            reports[(int(m.group("roc")) + ROC_OFFSET - 1, 4)] = ("annual_report", m.group(0))  # the report of ROC year 106 (2017) covers 2016
    return {**reports, **found}


class AdvantechIrProvider:
    def __init__(self, http: HttpClient | None, archive_dir: str | Path = "data/filings"):
        self.http = http
        self.archive_dir = Path(archive_dir)

    def _index(self, symbol: str) -> tuple[Path, list[dict]]:
        folder = self.archive_dir / symbol.upper()
        path = folder / "index.json"
        return folder, json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def release_info(self, symbol: str) -> dict[str, Release]:
        """The full years read from a shareholder annual report (url -> Release): the figures were public from the board's approval of the statements,
        the report itself came out later. Its publication day is not recorded; Taiwan companies must hold the annual general meeting, for which the report
        is published, by the end of June, so that is the upper bound `public_by`. Reads only the saved index."""
        _, index = self._index(symbol)
        out = {}
        for e in index:
            if e["kind"] == "annual_report":
                approved = pd.Timestamp(e["published"])
                out[e["url"]] = Release(approved, "financial statements approved by the board of directors", None, None, pd.Timestamp(year=approved.year, month=6, day=30))
        return out

    def results_announcements(self, symbol: str, since: datetime) -> list[Filing]:
        """All saved reports published after `since`, in ascending order of publication time (new ones are fetched first)."""
        folder, index = self._index(symbol)
        try:
            added = self._fetch_new(symbol, since, folder, index)
        except Exception as e:
            if not index:
                raise
            print(f"{symbol}: cannot reach the Advantech IR website ({e}); using the {len(index)} saved reports", file=sys.stderr)
            added = False
        if added:
            index.sort(key=lambda e: e["published"])
            save_index(folder / "index.json", index)

        since_ts = pd.Timestamp(since)
        filings = []
        for e in index:
            if pd.Timestamp(e["published"]) < since_ts:
                continue
            period = "full year" if e["quarter"] == 4 else f"Q{e['quarter']}"
            note = "; ".join([PUBLISHED_NOTE] + ([ANNUAL_REPORT_NOTE] if e["kind"] == "annual_report" else []))
            filings.append(
                Filing(
                    title=f"Advantech {e['year']} {period} {KIND_TITLES[e['kind']]}",
                    published=pd.Timestamp(e["published"]),
                    url=e["url"],
                    content=(folder / e["file"]).read_bytes(),
                    note=note,
                )
            )
        return filings

    def _fetch_new(self, symbol: str, since: datetime, folder: Path, index: list[dict]) -> bool:
        known = {(e["year"], e["quarter"]) for e in index}
        pages = [self.http.get_bytes(url).decode("utf-8", "replace") for url in PAGES]
        added = False
        for (year, quarter), (kind, url) in sorted(discover(pages).items()):
            period_end = pd.Period(year=year, quarter=quarter, freq="Q").end_time.normalize()
            if (year, quarter) in known or year < FIRST_YEAR or period_end + pd.Timedelta(days=120) < pd.Timestamp(since):
                continue
            pdf = self.http.get_bytes(url, cache=False)
            texts = page_texts(pdf)
            found = approval(texts, period_end)
            if found is None:
                print(f"{symbol}: {year}Q{quarter} ({url}) states no board approval date, so the publication time is unknown; skipped", file=sys.stderr)
                continue
            approved = found[0]
            name = f"{year}Q{quarter}_" + ("Shareholder-Annual-Report-" + url.split("/")[-2] + "-en.pdf" if kind == "annual_report" else url.rsplit("/", 1)[-1])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(excerpt(pdf, wanted_pages(texts, period_end)))
            index.append({"year": year, "quarter": quarter, "kind": kind, "published": f"{approved:%Y-%m-%d}T{PUBLISHED_TIME}:00", "url": url, "file": name})
            known.add((year, quarter))
            added = True
            print(f"{symbol}: new report {year}Q{quarter} ({kind}), approved by the board on {approved:%Y-%m-%d}", file=sys.stderr)
        return added
