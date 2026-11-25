"""SEC EDGAR: search for and download the original quarterly earnings press releases of US-listed (foreign private issuer) companies.

Earnings are furnished on Form 6-K by foreign private issuers (JD, ASML) and on Form 8-K item 2.02 by US companies (Meta, Cognizant, Dynatrace);
either way the press release is exhibit 99.1 (HTML). Two companies' press releases lack figures, so another exhibit is kept instead:
ASML's summary US GAAP statements and XP's IFRS consolidated financial statements (see `exhibit` in results_announcements). SAP's exhibit is its
quarterly results document, recognized by its statements rather than by a headline.
Flow: list the 6-Ks (or the 8-Ks with item 2.02) from the submissions API -> look at the file list of each and pick the press release exhibit of a plausible size ->
only a title like "... Announces ... Results" or "... Reports ... Results" is an earnings announcement.

Downloaded earnings filings are saved in data/filings/<symbol>/ (committed to the repository with the code); index.json there
records every filing that has been checked (including those that are not earnings), so they are not checked again. When EDGAR cannot be reached the locally saved filings are used.
XP's press release is read as well, for its adjusted EPS (results_announcements with supplement=True): every 6-K is checked once more for it, recorded in
releases.json next to index.json, so the selection of the statements in index.json is not touched.

EDGAR requires every request to carry a User-Agent that identifies a contact, and allows at most 10 requests per second.
The contact is not written in the code but provided through the SEC_USER_AGENT environment variable, e.g. "valuation-monitor your-email".
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from valuation_monitor.providers.base import Filing, save_index
from valuation_monitor.providers.http import HttpClient
from valuation_monitor.release_eps import is_results_release_title

USER_AGENT_ENV = "SEC_USER_AGENT"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{name}"
REQUEST_INTERVAL = 0.2  # seconds; the official limit is 10 requests per second
# Size of earnings press release exhibits: too small are notices, too large are annual reports or prospectuses (some 6-K exhibits exceed 15MB)
MIN_EXHIBIT_BYTES = 20_000
MAX_EXHIBIT_BYTES = 3_000_000
EXHIBIT_RE = re.compile(r"ex(?:hibit)?[-_ ]?99|press ?release", re.I)
# tried only after the names above found nothing: two Dynatrace 8-Ks name the exhibit fy21q2-earningsrelease.htm and fy20q1-earningsreleaseexhi.htm
FALLBACK_EXHIBIT_RE = re.compile(r"earnings[-_ ]?release", re.I)
# Title at the start of the press release, e.g. "JD.com Announces Second Quarter 2026 Results", "... Announces Fourth Quarter and Full Year 2025 Results"
EXHIBIT_LABEL_RE = re.compile(r"^.*?\bExhibi?t? ?99\.\d\s+", re.I)
# The Hong Kong-style results announcement that Alibaba files instead of a press release for the March and September quarters (since 2025), after
# a long Hong Kong Stock Exchange disclaimer: "ANNOUNCEMENT OF THE SEPTEMBER QUARTER 2025 RESULTS AND INTERIM RESULTS FOR THE SIX MONTHS ENDED SEPTEMBER 30, 2025"
HK_RESULTS_TITLE_RE = re.compile(r"\bANNOUNCEMENT OF THE [A-Z]+ QUARTER \d{4} RESULTS(?: AND [A-Z0-9 ]+? RESULTS(?: FOR THE [A-Z]+ MONTHS ENDED [A-Z]+ \d{1,2}, \d{4})?)?")
HK_TITLE_SEARCH_CHARS = 4000
# ASML's older releases say "today publishes its 2016 second-quarter results"; Amphenol's until 2020 "SECOND QUARTER 2017 RECORD RESULTS AND INCREASE
# IN DIVIDEND REPORTED BY AMPHENOL CORPORATION" (the passive form, with the quarter first)
RESULTS_HEADLINE_RE = re.compile(
    r"\b(?:announces?|reports?)\b.{0,120}?\b(?:results|net income|net sales)\b|\b(?:publishes|announces|reports) its\b.{0,60}?\bresults?\b"
    r"|\b(?:first|second|third|fourth) quarter\b.{0,80}?\bresults\b.{0,60}?\breported by \S+(?: corporation\b)?",
    re.I,
)


# an 8-K with item 2.02 that is not a results release although its text mentions reporting results: Apple's "Letter from Tim Cook to Apple
# Investors" of January 2, 2019 (revised guidance: "... before we complete and report our final results")
NOT_RESULTS_RE = re.compile(r"Letter from [^.]{1,60}? to [^.]{0,40}?\bInvestors\b", re.I)


class SecUserAgentMissing(RuntimeError):
    pass


class ArchiveNotAvailable(RuntimeError):
    """An EDGAR Archives file answered 404 (see INDEX_PAGE_NAME): the filing is tried again after the others, and if it still fails, on the next run."""


# EDGAR's CDN now and then answers a filing's index.json with a 404 page "Directory listing is Not Available Yet" and may keep serving it from
# its cache for minutes ("surrogate-control: max-age=300, stale-while-revalidate=120"), so retrying a few seconds later gets the same 404, with
# or without a query string. The filing's own index page ("<accession>-index.htm") lists the same documents with their sizes and is used instead.
SUPPLEMENT_EXHIBIT = "supplementary_release"  # an earnings press release kept next to the results of another exhibit (see results_announcements)
INDEX_PAGE_NAME = "{accession}-index.htm"


def make_http_client(cache_dir: str | Path = ".cache", ttl_seconds: int = 12 * 3600, refresh: bool = False) -> HttpClient:
    agent = os.environ.get(USER_AGENT_ENV, "").strip()
    if not agent:
        raise SecUserAgentMissing(
            f"The SEC requires you to identify yourself: set the environment variable {USER_AGENT_ENV} to \"program-name contact-email\", e.g. valuation-monitor you@example.com"
        )
    return HttpClient(cache_dir, ttl_seconds, refresh, user_agent=agent, min_interval=REQUEST_INTERVAL)


def to_new_york(accepted: str) -> pd.Timestamp:
    """acceptanceDateTime (UTC, e.g. 2026-08-13T10:05:29.000Z) -> New York local time (timezone-naive)."""
    return pd.Timestamp(accepted).tz_convert("America/New_York").tz_localize(None).floor("s")


FINANCIAL_STATEMENTS_NAME_RE = re.compile(r"financial_?statements", re.I)  # ASML: financialstatementsusgaapq.htm, financialstatementsusg.htm, financial_statementsxusx.htm
US_GAAP_STATEMENTS_RE = re.compile(r"Summary U\.?S\.? GAAP Consolidated Statements? of Operations|Financial Statements U\.?S\.? GAAP", re.I)  # the heading: at the very start of the exhibit


def statements_title(html: bytes) -> str | None:
    """"Summary US GAAP financial statements" if the exhibit is ASML's US GAAP summary (not the IFRS one, not a press release); None otherwise."""
    from bs4 import BeautifulSoup

    text = " ".join(BeautifulSoup(html[:200_000], "lxml").get_text(" ").split())[:500]
    return "Summary US GAAP financial statements" if US_GAAP_STATEMENTS_RE.search(text) else None


# XP: the IFRS statements are furnished in an exhibit (from 2022 on mostly in a 6-K of their own) that starts with the statements' heading
# ("XP Inc. Interim condensed consolidated financial statements at September 30, 2026"), the auditor's report on them, or the balance sheet itself
IFRS_START_RE = re.compile(
    r"(?:Unaudited )?(?:interim condensed )?consolidated (?:financial statements|balance sheets?)\b"
    r"|Report on review of interim condensed consolidated financial statements\b"
    r"|PricewaterhouseCoopers\b.{0,200}?Independent auditor.s report\b",
    re.I,
)
IFRS_START_CHARS = 120  # the heading comes first: "Exhibit 99.3 www.pwc.com.br XP Inc. Consolidated ...", "Exhibit 99.2 1 Report on review of ..."
IFRS_DATE_RE = re.compile(r"\bconsolidated (?:financial statements|balance sheets?)\b,? (?:at|as of|as at|for the years? ended) ([A-Z][a-z]+ \d{1,2}, \d{4})", re.I)
IFRS_MAX_EXHIBIT_BYTES = 6_000_000  # the audited annual statements with all their notes are 1-3MB
MIN_NUMBER_CELLS = 100  # statements in HTML tables have thousands of table cells with amounts


def has_number_tables(html: bytes) -> bool:
    """Whether the statements are HTML tables. XP's first annual statements (December 31, 2019) are page images with a hidden, garbled text
    layer ("7 , 15 3 , 3 9 6"), which cannot be read reliably: they are not taken (the 2019 figures come from the next year's statements)."""
    from bs4 import BeautifulSoup

    cells = BeautifulSoup(html, "lxml").find_all("td")
    return sum(1 for c in cells if re.fullmatch(r"\(?[\d.,]{3,}\)?", c.get_text(strip=True))) >= MIN_NUMBER_CELLS


def ifrs_statements_title(html: bytes) -> str | None:
    """A title such as "Interim condensed consolidated financial statements at September 30, 2026" if the exhibit is a set of IFRS
    consolidated financial statements; None otherwise.

    The exhibit must start with the statements (their heading, the auditor's report on them, or the balance sheet): a press release
    ("XP Inc. Reports ...") or a management's discussion ("... should be read in conjunction with our unaudited interim condensed consolidated
    financial statements as of ...") only refers to them later in the text. The date is that of the first balance sheet or heading.
    """
    from bs4 import BeautifulSoup

    text = " ".join(BeautifulSoup(html[:300_000], "lxml").get_text(" ").split())
    text = EXHIBIT_LABEL_RE.sub("", text, count=1)
    start = IFRS_START_RE.search(text[: IFRS_START_CHARS + 250])
    if start is not None and start.start() > IFRS_START_CHARS:
        start = None
    date = IFRS_DATE_RE.search(text[:200_000])
    if start is None or date is None:
        return None
    if not has_number_tables(html):
        return None
    interim = "interim" in (start.group(0) + date.group(0)).lower()
    return f"{'Interim condensed consolidated' if interim else 'Consolidated'} financial statements at {date.group(1)}"


def sap_statement_title(html: bytes) -> str | None:
    """A title such as "IFRS results for the three months ended September 30, 2026" if the exhibit is one of SAP's quarterly results documents
    with readable statements (see sap_ifrs.py): the footnote of the quarter's income statement and the equity row of the balance sheet; None otherwise.

    This leaves out the short preliminary press releases (no statements), the half-year reports furnished next to the quarterly statement (only a
    six-months footnote), annual reports, invitations and acquisition announcements, and the statements for the first quarter of 2020 and the
    fourth quarter of 2021, whose tables are images.
    """
    from valuation_monitor.sap_ifrs import EQUITY_RE, QUARTER_SHARES_RE, document_text

    text = document_text(html)
    footnote = QUARTER_SHARES_RE.search(text)
    if footnote is None or EQUITY_RE.search(text) is None:
        return None
    return f"IFRS results for the three months ended {footnote.group('month')} {footnote.group('day')}, {footnote.group('year')}"


def index_page_items(page: bytes) -> list[dict]:
    """The documents of the "Document Format Files" table of a filing's index page, as index.json lists them: {"name", "size"}, sorted by name."""
    from bs4 import BeautifulSoup

    table = BeautifulSoup(page, "lxml").find("table", class_="tableFile")
    if table is None:
        raise ValueError("The filing index page has no document table")
    items = []
    for tr in table.find_all("tr"):
        link, cells = tr.find("a"), [" ".join(td.get_text(" ").split()) for td in tr.find_all("td")]
        if link is None or len(cells) < 5:
            continue
        items.append({"name": link["href"].rsplit("/", 1)[-1], "size": cells[4] if cells[4].isdigit() else ""})
    return sorted(items, key=lambda item: item["name"].lower())  # index.json lists them by name, ignoring case


def headline(html: bytes) -> str | None:
    """The title at the start of the press release; None if it is not an earnings press release."""
    from bs4 import BeautifulSoup

    text = " ".join(BeautifulSoup(html[:200_000], "lxml").get_text(" ").split())
    if m := HK_RESULTS_TITLE_RE.search(text[:HK_TITLE_SEARCH_CHARS]):
        return m.group(0)
    text = EXHIBIT_LABEL_RE.sub("", text, count=1)[:1500]  # strip labels such as "EX-99.1 Exhibit 99.1" from the file header
    m = RESULTS_HEADLINE_RE.search(text)
    if m and NOT_RESULTS_RE.search(text[:200]):  # the title at the start, before the text of the letter
        return None
    if not m:
        return None
    return text[: m.end()].strip() if m.start() <= 60 else text[m.start() - 40 : m.end()].strip()


class SecProvider:
    def __init__(self, http: HttpClient | None, archive_dir: str | Path = "data/filings"):
        self.http = http
        self.archive_dir = Path(archive_dir)

    def results_announcements(
        self, symbol: str, cik: int, since: datetime, form: str = "6-K", exhibit: str = "release", supplement: bool = False
    ) -> list[Filing]:
        """All earnings releases published after `since`, in ascending order of publication time.

        `form` is the SEC form that carries them: 6-K or 8-K (item 2.02). `exhibit` is what is kept of each: "release", the press release itself, or
        "us_gaap_statements", the summary US GAAP financial statements exhibit (ASML: its press release has only a few figures, the statements have the rest),
        or "ifrs_statements", the IFRS consolidated financial statements (XP: furnished in a 6-K of their own; its press release has no basic EPS),
        or "sap_ifrs", SAP's quarterly results document with its IFRS statements (see sap_statement_title).
        With `supplement` the earnings press releases are looked for as supplementary documents of results parsed from another exhibit (XP: for the
        adjusted EPS of its press release), with an index of their own (releases.json) and only a title "... Reports ... Quarter ... Results" taken.
        """
        if supplement:
            exhibit = SUPPLEMENT_EXHIBIT
        folder = self.archive_dir / symbol.upper()
        index_path = folder / ("releases.json" if supplement else "index.json")
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        known = {e["accession"] for e in index}

        try:
            if self.http is None:
                raise SecUserAgentMissing(f"The environment variable {USER_AGENT_ENV} is not set")
            added = self._examine_new(symbol, cik, since, folder, index, known, form, exhibit)
        except Exception as e:
            if supplement and not index:  # supplementary documents are optional: without any saved, the results stand on the main filings alone
                print(f"{symbol}: cannot reach the SEC ({e}); no press releases saved for the adjusted EPS", file=sys.stderr)
                return []
            if not any(e_["results"] for e_ in index):
                raise
            print(f"{symbol}: cannot reach the SEC ({e}); using the {sum(x['results'] for x in index)} saved filings", file=sys.stderr)
            added = True  # the part already checked is written to the index as usual

        if added:
            index.sort(key=lambda e: e["published"])
            save_index(index_path, index)

        since_ts = pd.Timestamp(since)
        return [
            Filing(e["title"], pd.Timestamp(e["published"]), e["url"], (folder / e["file"]).read_bytes(), supplement=supplement)
            for e in index
            if e["results"] and pd.Timestamp(e["published"]) >= since_ts
        ]

    def _filing_list(self, cik: int, form: str) -> list[dict]:
        data = self.http.get_json(SUBMISSIONS_URL.format(name=f"CIK{cik:010d}.json"))
        blocks = [data["filings"]["recent"]]
        for extra in data["filings"].get("files", []):  # older records are split into other files
            blocks.append(self.http.get_json(SUBMISSIONS_URL.format(name=extra["name"])))
        out = []
        for b in blocks:
            items = b.get("items") or [""] * len(b["form"])
            for i, kind in enumerate(b["form"]):
                if kind == form and (form != "8-K" or "2.02" in items[i].split(",")):  # an 8-K is an earnings release only with item 2.02 (results of operations)
                    out.append({"accession": b["accessionNumber"][i], "published": to_new_york(b["acceptanceDateTime"][i])})
        return sorted(out, key=lambda x: x["published"])

    def _examine_new(self, symbol, cik, since, folder, index, known, form: str = "6-K", exhibit: str = "release") -> bool:
        added = False
        new = [item for item in self._filing_list(cik, form) if item["accession"] not in known and item["published"] >= pd.Timestamp(since)]
        for last_try in (False, True):
            skipped = []
            for item in new:
                try:
                    entry = self._examine(cik, item, folder, exhibit)
                except ArchiveNotAvailable as e:
                    if last_try:  # not recorded in the index, so the next run checks this filing again
                        print(f"{symbol}: skipping the filing {item['accession']} for this run ({e})", file=sys.stderr)
                    skipped.append(item)
                    continue
                index.append(entry)
                known.add(entry["accession"])
                added = True
                if entry["results"]:
                    print(f"{symbol}: new results release {entry['published']} {entry['title']}", file=sys.stderr)
            if not skipped:
                break
            new = skipped  # the second pass only tries again what answered 404, after all the others
        return added

    def _archive_get(self, url: str, **kwargs) -> bytes:
        """A file of the EDGAR Archives; a 404 raises ArchiveNotAvailable (the filing is then tried again later), other errors as they are."""
        try:
            return self.http.get_bytes(url, **kwargs)
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
            raise ArchiveNotAvailable(f"404 Not Found for {url}") from e

    def _listing(self, cik: int, accession: str) -> list[dict]:
        """The documents of a filing ({"name", "size"}), from its index.json, or from its index page when index.json answers 404 (see INDEX_PAGE_NAME)."""
        folder_name = accession.replace("-", "")
        try:
            return json.loads(self._archive_get(ARCHIVE_URL.format(cik=cik, accession=folder_name, name="index.json"), immutable=True))["directory"]["item"]
        except ArchiveNotAvailable:
            print(f"{accession}: index.json answered 404; reading the filing index page instead", file=sys.stderr)
            page = self._archive_get(ARCHIVE_URL.format(cik=cik, accession=folder_name, name=INDEX_PAGE_NAME.format(accession=accession)), immutable=True)
        return index_page_items(page)

    def _examine(self, cik: int, item: dict, folder: Path, exhibit: str = "release") -> dict:
        accession = item["accession"]
        folder_name = accession.replace("-", "")
        entry = {"accession": accession, "published": f"{item['published']:%Y-%m-%dT%H:%M:%S}", "results": False}
        files = self._listing(cik, accession)
        if exhibit == "us_gaap_statements":
            files = sorted(files, key=lambda f: not FINANCIAL_STATEMENTS_NAME_RE.search(f["name"]))  # the names say it from 2016 on (financialstatements...); before, the exhibits are numbered and are told apart by their content
        named = [f for f in files if EXHIBIT_RE.search(f["name"]) or (exhibit == "us_gaap_statements" and FINANCIAL_STATEMENTS_NAME_RE.search(f["name"]))]
        if exhibit == "release":
            named += [f for f in files if FALLBACK_EXHIBIT_RE.search(f["name"]) and f not in named]  # after all the usual names, so they are tried first as before
        for f in named:
            size = int(f["size"] or 0)
            name = f["name"]
            if not name.lower().endswith((".htm", ".html")):
                continue
            if not MIN_EXHIBIT_BYTES <= size <= (IFRS_MAX_EXHIBIT_BYTES if exhibit == "ifrs_statements" else MAX_EXHIBIT_BYTES):
                continue
            url = ARCHIVE_URL.format(cik=cik, accession=folder_name, name=name)
            html = self._archive_get(url, cache=False)
            if exhibit == "ifrs_statements":
                title = ifrs_statements_title(html)
            elif exhibit == "sap_ifrs":
                title = sap_statement_title(html)
            else:
                title = statements_title(html) if exhibit == "us_gaap_statements" else headline(html)
                if title and exhibit == SUPPLEMENT_EXHIBIT and not is_results_release_title(title):
                    title = None  # a notice of the results date, or preliminary estimates ("XP INC. ANNOUNCES PRELIMINARY ESTIMATED 2Q20 FINANCIAL RESULTS")
            if title:
                file = f"{item['published']:%Y-%m-%d}_{name}"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / file).write_bytes(html)
                return {**entry, "results": True, "title": title, "url": url, "file": file}
        return entry
