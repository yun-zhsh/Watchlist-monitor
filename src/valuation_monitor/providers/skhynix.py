"""SK Hynix IR website (skhynix.com/ir): English consolidated financial statements and earnings presentations.

The website is a single-page application; its pages are filled from a public JSON API (homeapi.skhynix.com/board/list), one "board" per page:
  - board 105 "Earnings Results": for each quarter since the fourth quarter of 2013, the date of the earnings conference call ("Oct 28, 2026/ 9:00 AM KST";
    the time is given from the first quarter of 2023 on) and the English earnings presentation (PDF).
  - board 101 "Audit (Review) Report": the English review reports (first three quarters) and audit reports (full year) of the consolidated financial
    statements (PDF), uploaded some weeks after the call.
The files are on the website's content delivery network ("filePath" of the API answer). The file names on the website are not used for the saved files
(some contain Korean words); the saved files are named <year>Q<quarter>_report.pdf or <year>Q<quarter>_presentation.pdf in data/filings/<symbol>/.

What is saved for each quarter (see skhynix.py for the parsing):
  - report        an excerpt of the review or audit report (the statements and the earnings per share note: about 5-8 of 40-100 pages);
  - presentation  an excerpt of the earnings presentation (the income statement summary and the capital structure page), when the report is not on the
                  website yet (provisional: replaced by the report once it is), or when the report on the website is a scanned image without a text
                  layer (1Q14, 1Q15, 3Q16, FY16, 1Q17, 2Q17, 1Q18 and 1Q19): then permanently.

Publication time. The results are first published on the day of the earnings call, before the market opens (the call is at 9:00 Korea time; the release
comes out shortly before it). The reports do not state a publication time and come out later, but their figures are those of the call (reviewed), so every
record keeps the time of the call, as is done for Samsung (see release_info). The time of day comes from, in this order:
  1. the IR website (from the first quarter of 2023 on);
  2. the list of earnings call times in CALL_TIMES_FILE (supplied by the maintainer from FactSet), used only when its date is the website's date;
  3. otherwise only the date is known, and the figures are taken as published after the 15:30 close: they count from the next trading day.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from pypdf import PdfReader, PdfWriter

from valuation_monitor.providers.base import Filing, Release, save_index
from valuation_monitor.providers.http import HttpClient
from valuation_monitor.skhynix import deck_pages, page_texts, parse_deck_texts, parse_report_texts, statement_pages

API = "https://homeapi.skhynix.com/board/list"
EARNINGS_BOARD = 105
REPORTS_BOARD = 101
FIRST_QUARTER = (2013, 4)  # the first quarter with an earnings call date on the website
CALL_TIMES_FILE = Path(__file__).with_name("skhynix_call_times.csv")
CALL_TIMES_COLUMNS = ["fiscal_year", "quarter", "date", "time", "time_zone", "source"]
SEOUL = ZoneInfo("Asia/Seoul")
DATE_ONLY_TIME = "15:30"  # the close of the Korea Exchange: published at the close counts from the next trading day
STATEMENTS_LAG_DAYS = 120  # the English reports have been seen 3 to 8 weeks after the call; after this many days they are taken to be public
SCANNED_TEXT_LIMIT = 2000  # characters of text in the whole report: below this it is a scanned image without a text layer
CALL_RE = re.compile(r"(?P<date>[A-Z][a-z]{2,8} \d{1,2}, 20\d\d)(?: ?/ ?(?P<hour>\d{1,2}):(?P<minute>\d\d) ?(?P<ampm>AM|PM) KST)?")
TITLE_RE = re.compile(r"FY(?P<year>20\d\d) Q(?P<q>[1-4]) Earnings? Results")
NOTES = {
    "website": "the time of the earnings call on the SK Hynix IR website (the results are published shortly before it, before the market opens)",
    "list": "the time of the earnings call from {source} (the IR website gives only its date)",
    "date": "the IR website gives only the date of the earnings call; taken as after the 15:30 close, so the figures count from the next trading day",
}
REPORT_NOTE = "the figures come from the English review or audit report, uploaded to the IR website weeks after the call; they are those published at the call, reviewed by the auditors"
SCANNED_NOTE = "the report on the IR website is a scanned image without a text layer, so the figures come from the earnings presentation"


def board(http: HttpClient, bcode: int) -> dict:
    return json.loads(http.get_bytes(API, params={"bcode": bcode, "lang": "ENG", "page": 1, "pageSize": 200}))


def attachments(entry: dict) -> list[tuple[str, str]]:
    """(file name, link path) of the files of a board entry."""
    return [(entry[f"fileName{i}"], entry[f"fileUrl{i}"]) for i in range(1, 6) if entry.get(f"fileName{i}") and entry.get(f"fileUrl{i}")]


def parse_calls(answer: dict) -> dict[tuple[int, int], dict]:
    """The earnings board: {(fiscal year, quarter): {"date": Timestamp (day), "time": "HH:MM" or None, "presentation": link or None}}."""
    out = {}
    for e in answer.get("list", []):
        title, when = TITLE_RE.search(e.get("title") or ""), CALL_RE.search(e.get("eventDate") or "")
        if not (title and when):
            continue
        time = None
        if when.group("hour"):
            hour = int(when.group("hour")) % 12 + (12 if when.group("ampm") == "PM" else 0)
            time = f"{hour:02d}:{when.group('minute')}"
        decks = [path for name, path in attachments(e) if name.lower().endswith(".pdf") and re.search(r"(?i)presentation", name)]
        out[(int(title.group("year")), int(title.group("q")))] = {
            "date": pd.Timestamp(when.group("date")),
            "time": time,
            "presentation": answer.get("filePath", "") + decks[0] if decks else None,
        }
    return out


def report_period(name: str, year: int) -> int | None:
    """The quarter of a consolidated report file of the reports board (4 = the audit report of the full year), from its name; None for another file."""
    if re.search(r"(?i)separate|non-consolidat", name) or not re.search(r"(?i)consolidat|quarterly.review|audit.report", name):
        return None
    if m := re.search(r"(?:^|[^\d])([1-3])Q(?:[^\d]|$)", name):
        return int(m.group(1))
    if re.search(r"(?i)4Q|audit", name):
        return 4
    return None


def parse_reports(answer: dict) -> dict[tuple[int, int], str]:
    """The reports board: {(fiscal year, quarter): link of the consolidated review or audit report}."""
    out = {}
    for e in answer.get("list", []):
        m = re.match(r"(20\d\d) Audit ?\(Review\) Report", e.get("title") or "")
        if not m:
            continue
        year = int(m.group(1))
        for name, path in attachments(e):
            if name.lower().endswith(".pdf") and (quarter := report_period(name, year)) is not None:
                out[(year, quarter)] = answer.get("filePath", "") + path
    return out


def read_call_times(path: Path = CALL_TIMES_FILE) -> dict[tuple[int, int], tuple[pd.Timestamp, str]]:
    """The maintainer's list of earnings call times: {(fiscal year, quarter): (Korea time, source)}. An empty dict when there is no list."""
    if not path.exists():
        return {}
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
    out = {}
    for r in rows:
        if list(r) != CALL_TIMES_COLUMNS:
            raise ValueError(f"{path.name}: expected the columns {','.join(CALL_TIMES_COLUMNS)}")
        local = pd.Timestamp(f"{r['date']} {r['time']}").tz_localize(ZoneInfo(r["time_zone"]))
        out[(int(r["fiscal_year"]), int(r["quarter"]))] = (local.tz_convert(SEOUL).tz_localize(None), r["source"])
    return out


def publication(call: dict, listed: tuple[pd.Timestamp, str] | None, symbol: str = "", period: str = "") -> tuple[pd.Timestamp, str]:
    """The publication time of a quarter (Korea time) and its explanation: the website's call time, else the listed one on the same day, else the close."""
    if call["time"]:
        return pd.Timestamp(f"{call['date']:%Y-%m-%d} {call['time']}"), NOTES["website"]
    if listed is not None:
        when, source = listed
        if when.normalize() == call["date"]:
            return when, NOTES["list"].format(source=source)
        print(f"{symbol}: {period} the listed call time {when} is not on the IR website's call date {call['date']:%Y-%m-%d}; ignored", file=sys.stderr)
    return pd.Timestamp(f"{call['date']:%Y-%m-%d} {DATE_ONLY_TIME}"), NOTES["date"]


def excerpt(pdf: bytes, pages: list[int]) -> bytes:
    """Keep only the given pages of the document (unchanged)."""
    reader = PdfReader(io.BytesIO(pdf))
    writer = PdfWriter()
    for i in pages:
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def title_of(year: int, quarter: int, kind: str) -> str:
    what = {"report": "consolidated financial statements", "presentation": "earnings presentation"}[kind]
    return f"SK Hynix {year} Q{quarter} {what}"


class SkHynixIrProvider:
    def __init__(self, http: HttpClient | None, archive_dir: str | Path = "data/filings", call_times: Path = CALL_TIMES_FILE):
        self.http = http
        self.archive_dir = Path(archive_dir)
        self.call_times = call_times

    def _index(self, symbol: str) -> tuple[Path, list[dict]]:
        folder = self.archive_dir / symbol.upper()
        path = folder / "index.json"
        return folder, json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def release_info(self, symbol: str) -> dict[str, Release]:
        """For each report: the figures were published at the earnings call, the report itself came out later (url -> Release).

        The upload day of the report is not recorded, so `public_by` is an upper bound: STATEMENTS_LAG_DAYS after the call. Reads only the saved index."""
        _, index = self._index(symbol)
        out = {}
        for e in index:
            if e["kind"] == "report":
                called = pd.Timestamp(e["published"])
                out[e["url"]] = Release(called, "earnings call", e.get("presentation"), None, called + pd.Timedelta(days=STATEMENTS_LAG_DAYS))
        return out

    def results_announcements(self, symbol: str, since: datetime) -> list[Filing]:
        """All saved quarters published after `since`, in ascending order of publication time (new ones are fetched first)."""
        folder, index = self._index(symbol)
        before = json.dumps(index, sort_keys=True)
        try:
            self._fetch_new(symbol, since, folder, index)
        except Exception as e:
            if not index:
                raise
            print(f"{symbol}: cannot reach the SK Hynix IR website ({e}); using the {len(index)} saved quarters", file=sys.stderr)
        if json.dumps(index, sort_keys=True) != before:  # what was fetched is recorded even when the website failed after it
            index.sort(key=lambda e: e["published"])
            save_index(folder / "index.json", index)

        since_ts = pd.Timestamp(since)
        filings = []
        for e in index:
            if pd.Timestamp(e["published"]) < since_ts:
                continue
            notes = [e["time_note"], REPORT_NOTE if e["kind"] == "report" else SCANNED_NOTE if not e.get("provisional") else ""]
            filings.append(
                Filing(
                    title=title_of(e["year"], e["quarter"], e["kind"]),
                    published=pd.Timestamp(e["published"]),
                    url=e["url"],
                    content=(folder / e["file"]).read_bytes(),
                    provisional=bool(e.get("provisional")),
                    note="; ".join(n for n in notes if n),
                )
            )
        return filings

    def _fetch_new(self, symbol: str, since: datetime, folder: Path, index: list[dict]) -> None:
        """Fetch the quarters not saved yet (and a report that replaces a provisional presentation), appending each to `index` as soon as its excerpt is
        written. A document that cannot be downloaded or read is an error for that quarter only: it is reported and tried again on the next update, and
        the other quarters are still fetched. Raises only when the boards of the IR website cannot be read."""
        calls = parse_calls(board(self.http, EARNINGS_BOARD))
        reports = parse_reports(board(self.http, REPORTS_BOARD))
        listed = read_call_times(self.call_times)
        since_ts = pd.Timestamp(since)
        saved = {(e["year"], e["quarter"]): e for e in index}
        for (year, quarter), call in sorted(calls.items()):
            period = f"{year}Q{quarter}"
            if (year, quarter) < FIRST_QUARTER:
                continue
            published, time_note = publication(call, listed.get((year, quarter)), symbol, period)
            old = saved.get((year, quarter))
            if old is not None and (old["published"], old["time_note"]) != (f"{published:%Y-%m-%dT%H:%M:%S}", time_note):
                old.update(published=f"{published:%Y-%m-%dT%H:%M:%S}", time_note=time_note)  # e.g. a call time listed since it was saved
                print(f"{symbol}: {period} publication time changed to {published} ({time_note})", file=sys.stderr)
            if published < since_ts or (old is not None and not old.get("provisional")):
                continue
            entry = {"year": year, "quarter": quarter, "published": f"{published:%Y-%m-%dT%H:%M:%S}", "time_note": time_note, "presentation": call["presentation"]}
            try:
                new = self._fetch_quarter(symbol, folder, entry, reports.get((year, quarter)), old)
            except Exception as e:
                print(f"ERROR {symbol}: {period} cannot be read ({type(e).__name__}: {e}); not saved, tried again on the next update", file=sys.stderr)
                continue
            if new is None:
                continue
            if old is not None:  # the report replaces the provisional presentation
                index.remove(old)
                (folder / old["file"]).unlink(missing_ok=True)
            index.append(new)
            saved[(year, quarter)] = new

    def _fetch_quarter(self, symbol: str, folder: Path, entry: dict, report: str | None, old: dict | None) -> dict | None:
        """Download the documents of one quarter and save the excerpt; returns its index entry, or None when nothing new is saved."""
        year, quarter = entry["year"], entry["quarter"]
        title = f"SK Hynix {year} Q{quarter}"
        scanned = False
        if report is not None:
            pdf = self.http.get_bytes(report, cache=False)
            texts = page_texts(pdf)
            if sum(len(t) for t in texts) < SCANNED_TEXT_LIMIT:
                scanned = True
            else:
                pages = statement_pages(texts)
                parse_report_texts(title, [texts[i] for i in pages])  # raises on a layout the parser cannot read: nothing is saved
                name = f"{year}Q{quarter}_report.pdf"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / name).write_bytes(excerpt(pdf, pages))
                print(f"{symbol}: new report {year}Q{quarter} (earnings call {entry['published']})", file=sys.stderr)
                return {**entry, "kind": "report", "url": report, "file": name}
        if old is not None and old.get("provisional"):  # the presentation is saved already
            if scanned:  # and the report that came out is a scanned image: the presentation stays, no longer as provisional data
                old["provisional"] = False
                print(f"{symbol}: {year}Q{quarter} report is a scanned image; keeping the earnings presentation ({entry['published']})", file=sys.stderr)
            return None  # otherwise still waiting for the report
        if entry["presentation"] is None:
            print(f"{symbol}: {year}Q{quarter} has neither a readable report nor an earnings presentation on the IR website; skipped", file=sys.stderr)
            return None
        pdf = self.http.get_bytes(entry["presentation"], cache=False)
        texts = page_texts(pdf)
        pages = deck_pages(texts)
        parse_deck_texts(title, [texts[i] for i in pages])
        name = f"{year}Q{quarter}_presentation.pdf"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(excerpt(pdf, pages))
        if scanned:
            print(f"{symbol}: {year}Q{quarter} report is a scanned image; using the earnings presentation ({entry['published']})", file=sys.stderr)
        else:
            print(f"{symbol}: the {year}Q{quarter} English report is not out yet; using the earnings presentation ({entry['published']}) for now", file=sys.stderr)
        return {**entry, "kind": "presentation", "url": entry["presentation"], "file": name, "provisional": not scanned}
