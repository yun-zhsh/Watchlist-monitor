"""SEC EDGAR: search for and download the original quarterly earnings press releases of US-listed (foreign private issuer) companies.

Earnings are furnished on Form 6-K by foreign private issuers (JD, ASML) and on Form 8-K item 2.02 by US companies (Meta, Cognizant, Dynatrace);
either way the press release is exhibit 99.1 (HTML). Two companies' press releases lack figures, so another exhibit is kept instead:
ASML's summary US GAAP statements and XP's IFRS consolidated financial statements (see `exhibit` in results_announcements). SAP's exhibit is its
quarterly results document, recognized by its statements rather than by a headline. Tesla's exhibit is its quarterly Update deck, recognized by
its title; for a quarter whose deck has its statements as page images only, the Form 10-Q or 10-K is kept as well (reports.json, see PERIODIC_REPORT).
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
# tried last: NVIDIA names its press release after the fiscal quarter, q3fy25pr.htm (q416pr.htm until fiscal 2016), next to q3fy25cfocommentary.htm
QUARTER_PR_EXHIBIT_RE = re.compile(r"^q[1-4](?:fy)?\d\dpr\.html?$", re.I)
# an 8-K with item 2.02 that only pre-announces figures is not an earnings release: "NVIDIA Announces Preliminary Financial Results" (August 2022)
PRELIMINARY_RE = re.compile(r"\bpreliminary\b", re.I)
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
    r"|\b(?:first|second|third|fourth) quarter\b.{0,80}?\bresults\b.{0,60}?\breported by \S+(?: corporation\b)?"
    # Amazon until the fourth quarter of 2019: "AMAZON.COM ANNOUNCES THIRD QUARTER SALES UP 24% TO $ 70.0 BILLION"
    r"|\bannounces (?:first|second|third|fourth) quarter sales (?:up|down)\b.{0,40}?\bbillion\b",
    re.I,
)
# Amazon's exhibits repeat their label before the headline, so a part is left after EXHIBIT_LABEL_RE: "Exhibit Exhibit 99.1 AMAZON.COM ANNOUNCES ...",
# "Document Exhibit 99.1 ...", "AMZN-2014.12.31-EX99.1 Exhibit 99.1 ..." (only before "AMAZON.COM": Sea's titles keep their "Exhibit 99.1")
AMAZON_LABEL_RE = re.compile(r"^(?:Exhibit |Document |AMZN-[\d.]+-EX99\.\d )?Exhibit 99\.\d (?=AMAZON\.COM )", re.I)

# Microsoft's headlines name no verb like "reports": "Microsoft Cloud Strength Highlights Second Quarter Results", "Microsoft Cloud Highlights Q3 Results",
# "Microsoft Cloud and Hardware Results Drives Fourth Quarter Performance"; tried only when RESULTS_HEADLINE_RE finds nothing, and taken only when the
# release then says "Microsoft Corp. today announced ... for the quarter ended"
MICROSOFT_HEADLINE_RE = re.compile(
    r"\b(Microsoft\b.{0,100}?\b(?:(?:first|second|third|fourth) quarter|Q[1-4]) (?:results|performance))\b(?=.{0,400}?\bMicrosoft Corp\. today announced\b.{0,120}?\bfor the quarter ended\b)",
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
TESLA_UPDATE = "tesla_update"  # Tesla's quarterly Update deck (exhibit 99.1 of its 8-K), see tesla_update.py
PERIODIC_REPORT = "periodic_report"  # the Form 10-Q or 10-K of a quarter whose Update has its statements as page images only (Tesla)
IMAGES_ONLY = "images"  # the "statements" of an index entry of an Update deck whose statements are page images only
INDEX_PAGE_NAME = "{accession}-index.htm"
# TSMC: the earnings release and the earnings conference slides, in the same 6-K (from the third quarter of 2019) or the slides in a 6-K of their
# own the next morning (until the second quarter of 2019); the documents have no telling names ("a3q26e_withguidancexfinal.htm", "d256764d6k.htm")
# and the slides are small (6-45 KB), so every HTML document of a 6-K is looked at (see tsmc_release.py and _examine_tsmc)
TSMC_RELEASE = "tsmc_release"
TSMC_MIN_BYTES = 1_000
SLIDES_NOTE = "earnings conference slides"  # Filing.note of a 6-K with only the slides: what its equity comes from (see build_history)


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


def tesla_update_title(html: bytes) -> str | None:
    """The title of a Tesla Update deck ("Q3 2026 Update", "Tesla Third Quarter 2016 Update", "Tesla Motors – First Quarter 2015 Shareholder
    Letter"); None for any other exhibit, such as the production and deliveries releases that Tesla also files under item 2.02."""
    from bs4 import BeautifulSoup

    from valuation_monitor.tesla_update import TITLE_RE

    text = " ".join(BeautifulSoup(html[:200_000], "lxml").get_text(" ").split())
    m = TITLE_RE.search(text[:TESLA_TITLE_CHARS])
    return m.group(0) if m else None


TESLA_EXHIBIT_RE = re.compile(r"exh?i?bit", re.I)  # "exhibit.htm" (Q2 and Q3 2024), "exhbit991.htm" (Q1 2025)
TESLA_TITLE_CHARS = 400  # the title comes first: "EX-99.1 2 tsla-ex991_57.htm EX-99.1 tsla-ex991_57.pptx.htm Q3 2020 Update Exhibit 99.1"
PERIODIC_COVER_CHARS = 100_000
PERIODIC_PERIOD_RE = re.compile(r"\bfor the (quarterly period|fiscal year) ended:? ([A-Z][a-z]+ \d{1,2}, ?\d{4})", re.I)


def periodic_report_title(html: bytes, form: str) -> str | None:
    """"Form 10-Q for the quarterly period ended September 30, 2021" or "Form 10-K for the fiscal year ended December 31, 2020", from the cover page."""
    import warnings

    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

    with warnings.catch_warnings():  # a 10-Q or 10-K in inline XBRL is XHTML; its text is all that is needed
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        text = " ".join(BeautifulSoup(html, "lxml").get_text(" ").split())  # inline XBRL: the hidden header with its contexts comes before the cover page
    m = PERIODIC_PERIOD_RE.search(text[:PERIODIC_COVER_CHARS])
    if m is None:
        return None
    return f"Form {form} for the {m.group(1).lower()} ended {m.group(2)}"


def headline(html: bytes) -> str | None:
    """The title at the start of the press release; None if it is not an earnings press release."""
    from bs4 import BeautifulSoup

    text = " ".join(BeautifulSoup(html[:200_000], "lxml").get_text(" ").split())
    if m := HK_RESULTS_TITLE_RE.search(text[:HK_TITLE_SEARCH_CHARS]):
        return m.group(0)
    text = EXHIBIT_LABEL_RE.sub("", text, count=1)[:1500]  # strip labels such as "EX-99.1 Exhibit 99.1" from the file header
    text = AMAZON_LABEL_RE.sub("", text, count=1)
    m = RESULTS_HEADLINE_RE.search(text)
    if m and NOT_RESULTS_RE.search(text[:200]):  # the title at the start, before the text of the letter
        return None
    if not m:
        if m := MICROSOFT_HEADLINE_RE.search(text):
            return m.group(1)
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
        index_path = folder / ("releases.json" if supplement else "reports.json" if exhibit == PERIODIC_REPORT else "index.json")
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        known = {e["accession"] for e in index}
        # the Form 10-Q or 10-K only of a quarter whose Update deck (in index.json) has its statements as page images; the others are not recorded
        wanted = image_only_quarters(folder / "index.json") if exhibit == PERIODIC_REPORT else None

        try:
            if self.http is None:
                raise SecUserAgentMissing(f"The environment variable {USER_AGENT_ENV} is not set")
            added = self._examine_new(symbol, cik, since, folder, index, known, form, exhibit, wanted)
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
        if exhibit == TSMC_RELEASE:
            statements = self._tsmc_statements(symbol, cik, since_ts, folder, index)
            return tsmc_filings(index, folder, since_ts) + statements
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
            documents, report_dates = b.get("primaryDocument") or [""] * len(b["form"]), b.get("reportDate") or [""] * len(b["form"])
            for i, kind in enumerate(b["form"]):
                if kind in form.split("/") and (kind != "8-K" or "2.02" in items[i].split(",")):  # an 8-K is an earnings release only with item 2.02 (results of operations)
                    out.append({"accession": b["accessionNumber"][i], "published": to_new_york(b["acceptanceDateTime"][i])})
                    if kind in PERIODIC_FORMS:  # a 10-Q or 10-K (form "10-Q/10-K"): the report itself is the primary document
                        out[-1].update(form=kind, document=documents[i], report_date=report_dates[i])
        return sorted(out, key=lambda x: x["published"])

    def _examine_new(self, symbol, cik, since, folder, index, known, form: str = "6-K", exhibit: str = "release", wanted: set[pd.Period] | None = None) -> bool:
        added = False
        new = [item for item in self._filing_list(cik, form) if item["accession"] not in known and item["published"] >= pd.Timestamp(since)]
        if wanted is not None:  # periodic reports: only those of the wanted quarters
            new = [item for item in new if item.get("report_date") and pd.Period(item["report_date"], freq="Q") in wanted]
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
        if exhibit == PERIODIC_REPORT:
            return self._examine_report(cik, item, folder, entry)
        if exhibit == TSMC_RELEASE:
            return self._examine_tsmc(cik, item, folder, entry)
        files = self._listing(cik, accession)
        if exhibit == "us_gaap_statements":
            files = sorted(files, key=lambda f: not FINANCIAL_STATEMENTS_NAME_RE.search(f["name"]))  # the names say it from 2016 on (financialstatements...); before, the exhibits are numbered and are told apart by their content
        named = [f for f in files if EXHIBIT_RE.search(f["name"]) or (exhibit == "us_gaap_statements" and FINANCIAL_STATEMENTS_NAME_RE.search(f["name"]))]
        if exhibit == "release":
            named += [f for f in files if FALLBACK_EXHIBIT_RE.search(f["name"]) and f not in named]  # after all the usual names, so they are tried first as before
            named += [f for f in files if QUARTER_PR_EXHIBIT_RE.search(f["name"]) and f not in named]  # NVIDIA, after all the others
        if exhibit == TESLA_UPDATE:
            named += [f for f in files if TESLA_EXHIBIT_RE.search(f["name"]) and f not in named]  # Tesla's decks without "99" in the name, after the usual names
        for f in named:
            size = int(f["size"] or 0)
            name = f["name"]
            if not name.lower().endswith((".htm", ".html")):
                continue
            if not MIN_EXHIBIT_BYTES <= size <= (IFRS_MAX_EXHIBIT_BYTES if exhibit == "ifrs_statements" else MAX_EXHIBIT_BYTES):
                continue
            url = ARCHIVE_URL.format(cik=cik, accession=folder_name, name=name)
            html = self._archive_get(url, cache=False)
            if exhibit == TESLA_UPDATE:
                from valuation_monitor.tesla_update import statements_in_text

                title = tesla_update_title(html)
                if title and not statements_in_text(html):  # recorded, so that the quarter's Form 10-Q or 10-K is read instead (PERIODIC_REPORT)
                    return {**entry, "title": title, "statements": IMAGES_ONLY}
            elif exhibit == "ifrs_statements":
                title = ifrs_statements_title(html)
            elif exhibit == "sap_ifrs":
                title = sap_statement_title(html)
            else:
                title = statements_title(html) if exhibit == "us_gaap_statements" else headline(html)
                if title and exhibit == SUPPLEMENT_EXHIBIT and not is_results_release_title(title):
                    title = None  # a notice of the results date, or preliminary estimates ("XP INC. ANNOUNCES PRELIMINARY ESTIMATED 2Q20 FINANCIAL RESULTS")
                if title and exhibit == "release" and PRELIMINARY_RE.search(title):
                    title = None  # preliminary figures announced ahead of the release (NVIDIA, August 2022)
            if title:
                file = f"{item['published']:%Y-%m-%d}_{name}"
                folder.mkdir(parents=True, exist_ok=True)
                (folder / file).write_bytes(html)
                return {**entry, "results": True, "title": title, "url": url, "file": file}
        return entry

    def _examine_report(self, cik: int, item: dict, folder: Path, entry: dict) -> dict:
        """A Form 10-Q or 10-K: its primary document, kept when its cover page names the period of the report."""
        name = item["document"]
        url = ARCHIVE_URL.format(cik=cik, accession=item["accession"].replace("-", ""), name=name)
        html = self._archive_get(url, cache=False)
        title = periodic_report_title(html, item["form"])
        if title is None:
            return entry
        file = f"{item['published']:%Y-%m-%d}_{name}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / file).write_bytes(html)
        return {**entry, "results": True, "title": title, "url": url, "file": file}


    def _examine_tsmc(self, cik: int, item: dict, folder: Path, entry: dict) -> dict:
        """A TSMC 6-K: its earnings release ("TSMC Reports ... Quarter EPS of NT$...") and its earnings conference slides with a text balance
        sheet, whichever it has. The slides are recorded as "slides_file" next to the release, or alone (results false) when the 6-K has only them."""
        from valuation_monitor.tsmc_release import has_text_balance_sheet, slides_title, title as release_title

        accession = item["accession"]
        folder_name = accession.replace("-", "")
        found: dict[str, str] = {}
        for f in self._listing(cik, accession):
            name, size = f["name"], int(f["size"] or 0)
            if not name.lower().endswith((".htm", ".html")) or name.endswith("-index.htm") or name.endswith("-index-headers.html"):
                continue
            if not TSMC_MIN_BYTES <= size <= MAX_EXHIBIT_BYTES:
                continue
            url = ARCHIVE_URL.format(cik=cik, accession=folder_name, name=name)
            html = self._archive_get(url, cache=False)
            file = f"{item['published']:%Y-%m-%d}_{name}"
            if "file" not in found and (title := release_title(html)):
                found.update(title=title, url=url, file=file)
            elif "slides_file" not in found and (title := slides_title(html)) and has_text_balance_sheet(html):
                found.update(slides_title=title, slides_url=url, slides_file=file)
            else:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            (folder / file).write_bytes(html)
        return {**entry, "results": "file" in found, **found}


    def _tsmc_statements(self, symbol: str, cik: int, since: pd.Timestamp, folder: Path, index: list[dict]) -> list[Filing]:
        """TSMC's consolidated financial statements of the quarters whose slides have no text balance sheet (see tsmc_equity_gaps), as
        supplementary documents for their equity, from statements.json next to index.json. The 6-Ks published 15-100 days after the end of
        such a quarter are looked at, each recorded in statements.json as it is checked (the index is saved whenever it grew); a 6-K that
        cannot be read is reported and tried again on the next run, and the others go on."""
        from valuation_monitor.tsmc_release import STATEMENTS_NOTE, statements_excerpt, statements_period, statements_title

        path = folder / "statements.json"
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        wanted = {q for q in tsmc_equity_gaps(index, folder) if q.end_time >= since} - {statements_period(e["title"]) for e in saved if e["results"]}
        if wanted and self.http is not None:
            known = {e["accession"] for e in saved}
            try:
                candidates = [
                    item for item in self._filing_list(cik, "6-K")
                    if item["accession"] not in known and any(STATEMENTS_WINDOW_DAYS[0] <= (item["published"] - q.end_time.normalize()).days <= STATEMENTS_WINDOW_DAYS[1] for q in wanted)
                ]
            except Exception as e:  # noqa: BLE001 - the saved statements are used
                print(f"{symbol}: cannot reach the SEC ({e}); using the {sum(x['results'] for x in saved)} saved financial statements", file=sys.stderr)
                candidates = []
            for item in candidates:
                entry = {"accession": item["accession"], "published": f"{item['published']:%Y-%m-%dT%H:%M:%S}", "results": False}
                try:
                    for f in self._listing(cik, item["accession"]):
                        name, size = f["name"], int(f["size"] or 0)
                        if not name.lower().endswith((".htm", ".html")) or not STATEMENTS_MIN_BYTES <= size <= STATEMENTS_MAX_BYTES:
                            continue
                        url = ARCHIVE_URL.format(cik=cik, accession=item["accession"].replace("-", ""), name=name)
                        html = self._archive_get(url, cache=False)
                        title = statements_title(html)
                        if title is None or statements_period(title) not in wanted:
                            continue
                        file = f"{item['published']:%Y-%m-%d}_{name}"
                        folder.mkdir(parents=True, exist_ok=True)
                        (folder / file).write_bytes(statements_excerpt(html))
                        entry.update(results=True, title=title, url=url, file=file)
                        wanted.discard(statements_period(title))
                        print(f"{symbol}: new consolidated financial statements {entry['published']} {title}", file=sys.stderr)
                        break
                except Exception as e:  # noqa: BLE001 - this 6-K only; not recorded, so the next run checks it again
                    print(f"{symbol}: ERROR reading the 6-K {item['accession']} for financial statements ({e}); skipped for this run", file=sys.stderr)
                    continue
                saved.append(entry)
                saved.sort(key=lambda e: e["published"])
                save_index(path, saved)
                if not wanted:
                    break
        return [
            Filing(e["title"], pd.Timestamp(e["published"]), e["url"], (folder / e["file"]).read_bytes(), note=STATEMENTS_NOTE, supplement=True)
            for e in saved
            if e["results"] and pd.Timestamp(e["published"]) >= since
        ]


STATEMENTS_WINDOW_DAYS = (15, 100)  # TSMC furnishes its consolidated financial statements 4-8 weeks after the quarter end
STATEMENTS_MIN_BYTES = 1_000_000  # the statements with their notes are 1.5-12 MB; the 6-K cover and the notices are far smaller
STATEMENTS_MAX_BYTES = 20_000_000


def tsmc_equity_gaps(index: list[dict], folder: Path) -> set[pd.Period]:
    """The quarters of TSMC's earnings releases (index.json) whose equity is in no text balance sheet: neither slides of the same 6-K, nor
    slides in a 6-K of their own, nor the release document itself (its quarter: 45 days before its publication, as us_release.quarter_of)."""
    from valuation_monitor.tsmc_release import has_text_balance_sheet

    def quarter(e: dict) -> pd.Period:
        return (pd.Timestamp(e["published"]) - pd.Timedelta(days=45)).to_period("Q")

    covered = {quarter(e) for e in index if e.get("slides_file")}
    return {
        quarter(e) for e in index
        if e["results"] and quarter(e) not in covered and not has_text_balance_sheet((folder / e["file"]).read_bytes())
    }


def tsmc_filings(index: list[dict], folder: Path, since: pd.Timestamp) -> list[Filing]:
    """TSMC's filings from index.json: each earnings release, with the slides of the same 6-K as the attachment "slides", and each 6-K that has
    only the slides as a supplementary document (its equity is used from its own publication time, see build_history)."""
    out = []
    for e in index:
        if pd.Timestamp(e["published"]) < since:
            continue
        if e["results"]:
            attachments = {"slides": (folder / e["slides_file"]).read_bytes()} if e.get("slides_file") else {}
            out.append(Filing(e["title"], pd.Timestamp(e["published"]), e["url"], (folder / e["file"]).read_bytes(), attachments))
        elif e.get("slides_file"):
            out.append(Filing(e["slides_title"], pd.Timestamp(e["published"]), e["slides_url"], (folder / e["slides_file"]).read_bytes(), note=SLIDES_NOTE, supplement=True))
    return out


PERIODIC_FORMS = ("10-Q", "10-K")


def image_only_quarters(index_path: Path) -> set[pd.Period]:
    """The quarters whose Update deck in this index.json has its statements as page images only (Tesla; see TESLA_UPDATE)."""
    from valuation_monitor.tesla_update import title_quarter

    if not index_path.exists():
        return set()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    return {title_quarter(e["title"]) for e in index if e.get("statements") == IMAGES_ONLY}
