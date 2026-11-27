"""Extract earnings data from Advantech's English consolidated financial statements (Taiwan-IFRS, PDF; see providers/advantech.py).

Verified on every report saved since 2015 (two layouts: Deloitte & Touche up to the first quarter of 2021, PricewaterhouseCoopers since, which
puts the Taiwan-IFRS account codes such as 4000, 8610 and 31XX in front of the rows). Amounts are in thousands of New Taiwan dollars; EPS in dollars.
  - Consolidated statement of comprehensive income (the pages headed "... AND SUBSIDIARIES CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME"):
    operating revenue, profit attributable to owners of the parent and basic EPS. The first three quarters have the three months and the year to
    date, each for this year and last year; the three-month columns are used. The annual statements have the two full years; the fourth quarter is
    then obtained by the program as full year minus the first three quarters.
  - Consolidated balance sheet: equity attributable to owners of the parent (there are no preference shares), for the end of this period and
    the end of last year (the columns come in that order; interim balance sheets also have the same day last year, which is not used).
  - EPS note: the weighted average number of ordinary shares used for basic EPS (thousands): the first row in the note is the one of the three
    months (or the full year), in the same order as the statements.
The shareholder annual report (the full years 2015-2017) contains the parent-company-only statements as well; only the pages of the consolidated
statements, which carry "AND SUBSIDIARIES" in their heading, and the notes after them are read.
"""

from __future__ import annotations

import io
import re
import sys

import pandas as pd
from pypdf import PdfReader

from valuation_monitor.parsed import Parsed

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE = rf"(?P<date>(?:{MONTHS}) \d{{1,2}}, ?20\d\d)"
# "approved by the board of directors on March 8, 2019", "approved by the board of directors April 29, 2016",
# "approved by the board of directors and authorized for issue on May 3, 2013", "approved by the Company's board of directors on August 2, 2019", "authorised for issuance by the Board of Directors on October 29, 2026"
APPROVAL_RE = re.compile(
    rf"(?:approved by the (?:Company.s )?board of directors(?: and authori[sz]ed for issue)?|authori[sz]ed for issu(?:e|ance) by the board of directors)(?: on)? {DATE}", re.I
)
APPROVAL_WINDOW_DAYS = (15, 100)  # the statements are approved within 45 days of a quarter end (90 days of the year end) by law


def page_texts(pdf: bytes) -> list[str]:
    """The text of each page, whitespace collapsed to single spaces."""
    return [" ".join((p.extract_text() or "").split()) for p in PdfReader(io.BytesIO(pdf)).pages]


def approval(texts: list[str], period_end: pd.Timestamp) -> tuple[pd.Timestamp, int] | None:
    """The day the board of directors approved the statements of the period ending `period_end`, and the page that says so: the first
    approval sentence whose date is 15-100 days after the period end (other sentences of the kind are about earlier resolutions, e.g. a
    dividend). None if there is none."""
    for page, text in enumerate(texts):
        for m in APPROVAL_RE.finditer(text):
            day = pd.Timestamp(m.group("date").replace(",", ", ").replace(",  ", ", "))
            if APPROVAL_WINDOW_DAYS[0] <= (day - period_end.normalize()).days <= APPROVAL_WINDOW_DAYS[1]:
                return day, page
    return None


# Amounts have thousands separators: percentages and note numbers, which do not, are never taken. A token that looks like an amount but has
# misplaced separators is a misprint in the filing ("26,26,290" in the 2026 second-quarter income statement) and is refused, never skipped.
AMOUNT = re.compile(r"\(?-?\d{1,3}(?:,\d{3})+\)?")
NUMBER_LIKE = re.compile(r"\(?-?\d+(?:,\d+)+\)?")
EPS_AMOUNT = r"\(? ?-?\d+\.\d\d ?\)?"
TITLE_RE = re.compile(r"Advantech (?P<year>20\d\d) (?:Q(?P<q>[1-3])|(?P<fy>full year))")
INCOME_HEADING = "ANDSUBSIDIARIESCONSOLIDATEDSTATEMENTSOFCOMPREHENSIVEINCOME"
BALANCE_HEADING = "ANDSUBSIDIARIESCONSOLIDATEDBALANCESHEETS"
HEADING_CHARS = 400  # the heading is at the top of the page (after the page number)
REVENUE_RE = re.compile(r"(?:Total operating revenue|\b4000 Operating revenue)\b", re.I)
PROFIT_RE = re.compile(
    r"(?:NET )?PROFIT (?:\(LOSS\) )?ATTRIBUTABLE TO:? (?:8610 )?(?:Owners of the (?:Company|parent)|Shareholders of the parent)", re.I
)
EPS_RE = re.compile(rf"(?:EARNINGS PER SHARE(?: \([^)]*\))? Basic|Basic earnings per share)[^$]{{0,60}}?\$ ?(?P<a>{EPS_AMOUNT}) ?\$ ?(?P<b>{EPS_AMOUNT})", re.I)
# the row with the total, not the heading of the section (which is followed by "Share capital"); the account code 31XX is sometimes cut to "1XX" in the text
EQUITY_RE = re.compile(
    r"(?:Total equity attributable to owners of the Company|(?:Total )?Equity attributable to (?:owners|shareholders) of the parent)(?= \(?\d{1,3},\d{3})", re.I
)
SHARES_RE = re.compile(r"Weighted average number of ordinary shares (?:outstanding )?(?:used )?in (?:the )?computation of basic earnings per share", re.I)
# the revenue note, used when the revenue row of the income statement is misprinted: its first table is the period of the statement's first column
REVENUE_NOTE_RE = re.compile(r"Revenue from contracts with customers(?= \$? ?\(?\d{1,3},\d{3})", re.I)
SHARES_TOLERANCE = 0.02  # the disclosed weighted average shares must be within 2% of net profit / basic EPS


def to_number(token: str) -> float:
    value = float(token.replace(" ", "").strip("()").replace(",", ""))
    return -value if token.startswith("(") else value


def squeeze(text: str) -> str:
    return re.sub(r"\s", "", text).upper()


def statement_pages(texts: list[str], heading: str) -> list[int]:
    """The pages of the first consolidated statement with this heading (the statement runs over consecutive pages)."""
    pages = [i for i, t in enumerate(texts) if heading in squeeze(t[:HEADING_CHARS])]
    if not pages:
        return []
    run = [pages[0]]
    for i in pages[1:]:
        if i != run[-1] + 1:
            break
        run.append(i)
    return run


class MisprintedAmount(ValueError):
    pass


def amounts_after(text: str, start: int, count: int, name: str) -> list[float]:
    """The first `count` amounts after `start` (thousands separators required), on the same row: stops at the next account code or label."""
    out = []
    for token in re.sub(r"\( ?([\d,]+) ?\)", r"(\1)", text[start:]).replace("$", " ").split()[:4 * count + 12]:
        if AMOUNT.fullmatch(token):
            out.append(to_number(token))
            if len(out) == count:
                return out
        elif NUMBER_LIKE.fullmatch(token):
            raise MisprintedAmount(f"{name}: the amount {token!r} in the filing is misprinted")
    raise ValueError(f"{name}: fewer than {count} amounts in the row")


def row(regex: re.Pattern, text: str, name: str, count: int = 2) -> list[float]:
    m = regex.search(text)
    if not m:
        raise ValueError(f"Not found in the statements: {name}")
    return amounts_after(text, m.end(), count, name)


def parse_tifrs(title: str, pdf: bytes, texts: list[str] | None = None) -> Parsed:
    """One report: the quarter (three-month columns) or the full year (annual statements, shareholder annual report)."""
    m = TITLE_RE.search(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    year, quarter = int(m.group("year")), 4 if m.group("fy") else int(m.group("q"))
    texts = texts if texts is not None else page_texts(pdf)
    income_pages, balance_pages = statement_pages(texts, INCOME_HEADING), statement_pages(texts, BALANCE_HEADING)
    if not income_pages or not balance_pages:
        raise ValueError(f"{title}: the consolidated statement of comprehensive income or balance sheet was not found")
    income = " ".join(texts[i] for i in income_pages)
    balance = " ".join(texts[i] for i in balance_pages)
    period = pd.Period(year=year, quarter=quarter, freq="Q")
    end = period.end_time
    if str(year) not in income or str(year - 1) not in income or f"{end:%B} {end.day}".upper() not in income.upper().replace("  ", " "):
        raise ValueError(f"{title}: the income statement is not for the period ended {end:%B} {end.day}, {year} (and {year - 1})")

    notes = {}
    try:
        revenue = row(REVENUE_RE, income, "Operating revenue")
    except MisprintedAmount as e:
        after = " ".join(texts[income_pages[-1] + 1:])
        revenue = row(REVENUE_NOTE_RE, after, "Revenue from contracts with customers (revenue note)")
        notes["revenue"] = f"{e}; the figure is taken from the revenue note (revenue from contracts with customers)"
    values = {
        "revenue": revenue,
        "profit": row(PROFIT_RE, income, "Profit attributable to owners of the parent"),
        "eps": [],
        "equity": row(EQUITY_RE, balance, "Equity attributable to owners of the parent"),
    }
    eps = EPS_RE.search(income)
    if not eps:
        raise ValueError("Not found in the statements: Basic EPS")
    values["eps"] = [to_number(eps.group("a")), to_number(eps.group("b"))]

    notes_text = " ".join(texts[income_pages[-1] + 1:])
    shares = SHARES_RE.search(notes_text)
    if shares:
        wa = [v / 1000 for v in amounts_after(notes_text, shares.end(), 2, "Weighted average shares")]  # thousands -> millions
        implied = values["profit"][0] / values["eps"][0] / 1000
        if abs(wa[0] / implied - 1) <= SHARES_TOLERANCE:
            values["wa_shares"] = wa
        else:
            print(f"{title}: the weighted average shares in the EPS note ({wa[0]:,.1f} million) are not near profit / EPS ({implied:,.1f} million); shares are implied instead", file=sys.stderr)
    values = {k: v if k in ("eps", "wa_shares") else [x / 1000 for x in v] for k, v in values.items()}  # thousands of TWD -> millions
    return Parsed("TWD", values, period, quarterly=quarter != 4, notes=notes or None)


def first_page_after(texts: list[str], start: int, regex: re.Pattern) -> int | None:
    return next((i for i in range(start, len(texts)) if regex.search(texts[i])), None)


def wanted_pages(texts: list[str], period_end: pd.Timestamp) -> list[int]:
    """The pages the parser reads, to save an excerpt of the report (see providers/advantech.py): the cover, the page stating the board's approval,
    the consolidated statement of comprehensive income and balance sheet, and after them the first page with the EPS note's share count and the
    first with the revenue note (each with the next page, where a table may continue). The pages stay unchanged."""
    income = statement_pages(texts, INCOME_HEADING)
    wanted = {0, *income, *statement_pages(texts, BALANCE_HEADING)}
    if found := approval(texts, period_end):
        wanted.add(found[1])
    if income:
        for regex in (SHARES_RE, REVENUE_NOTE_RE):
            if (page := first_page_after(texts, income[-1] + 1, regex)) is not None:
                wanted.update({page, page + 1})
    return sorted(w for w in wanted if w < len(texts))
