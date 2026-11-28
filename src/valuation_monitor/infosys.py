"""Extract earnings data from Infosys's IFRS documents in US dollars furnished on Form 6-K (millions of US dollars).

Infosys furnishes two 6-Ks a quarter, both with exhibits named only by their number ("exv99w02.htm"):

  release      on the day of the results (Infosys publishes after the close in India, before the US open), the 6-K with the "IFRS USD Press Release".
               It ends with figures extracted from the IFRS statements: the condensed consolidated balance sheet (this quarter end, the end of
               the last fiscal year) and the condensed consolidated statement of comprehensive income ("3 months ended" this quarter and the same
               quarter last year, then the year-to-date or full-year columns). Revenue is "Revenues", net profit "Net profit (after non-controlling
               interest)", EPS "Basic EPS ($)" (two decimals), equity "Total equity attributable to equity holders of the company".
               The press release has no share count, so until the statements arrive the shares are implied (net profit / basic EPS).
  statements   4-7 days later, a 6-K with all the documents of the quarter, among them the audited interim condensed consolidated financial
               statements in compliance with IFRS in US dollars (and the same press release again). Their statement of comprehensive income has
               the "Weighted average equity shares used in computing earnings per equity share", basic (in shares), for the three months of this
               quarter and the same quarter last year; the fourth quarter has its three months too ("Three months ended", "Year ended").
               Only the start of the document up to that table is kept (statements_excerpt). The statements are a supplementary document of the
               release (see filings.build_history): they add the disclosed shares, from their own publication time.

The same value in two tables must agree and a missing value is an error: nothing is guessed.
"""

from __future__ import annotations

import re

import pandas as pd

from valuation_monitor.ifrs import clean, table_rows, to_number
from valuation_monitor.parsed import Parsed

CURRENCY = "USD"
STATEMENTS_NOTE = "IFRS statements"  # Filing.note of the statements: what their figures come from (see build_history)
IMPLIED_SHARES_NOTE = (
    "shares implied from net profit / basic EPS (two decimals) until the IFRS statements are published (the press release has no share count)"
)
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
DATE = r"(?P<month>" + "|".join(MONTHS) + r") (?P<day>\d{1,2}), (?P<year>\d{4})"
EXHIBIT_LABEL_RE = re.compile(r"^.*?\bExhibit 99\.\d+\s+", re.I)
RELEASE_START_RE = re.compile(r"^IFRS\s*[–-]?\s*USD Press Release\b", re.I)  # "IFRS – USD Press Release" since July 2026
RELEASE_START_CHARS = 200
# Income statement headers: one row of "3 months ended September 30, 2025" cells (the press release), or a row of groups ("Three months ended",
# "Three months ended December 31,") over a row of dates or years (the statements). "Note", "Particulars", the statement's title and
# "Unaudited" / "Audited" are left out.
COLUMN_RE = re.compile(r"^(?:(?P<kind>3|6|9|three|six|nine) months|years?) ended " + DATE + "$", re.I)
GROUP_RE = re.compile(r"^(?:(?P<kind>3|6|9|three|six|nine) months|years?) ended(?: (?P<month>" + "|".join(MONTHS) + r") (?P<day>\d{1,2}),?)?$", re.I)
DATE_CELL_RE = re.compile(r"^(?:As at |As of )?" + DATE + "$", re.I)
YEAR_CELL_RE = re.compile(r"^(?:19|20)\d\d$")
IGNORED_HEADER_RE = re.compile(r"^(?:Note|Particulars|Unaudited|Audited|Condensed Consolidated .*)$", re.I)
STATEMENTS_RE = re.compile(r"\(Dollars in millions except (?:equity )?share and per (?:equity )?share data\)", re.I)
STATEMENTS_HEADING_RE = re.compile(r"Consolidated (?:Interim )?Statements? of Comprehensive Income", re.I)
RAW_SHARES_RE = re.compile(rb"Weighted\s+average\s+equity\s+shares\s+used\s+in\s+computing\s+earnings\s+per\s+equity\s+share", re.I)
RAW_TABLE_END_RE = re.compile(rb"</table\s*>", re.I)

REVENUE_RE = re.compile(r"^Revenues?$", re.I)
PROFIT_RE = re.compile(r"^Net profit \(after (?:non-controlling interests?|minority interests?)\)$", re.I)  # the press release since 2020
OWNERS_HEAD_RE = re.compile(r"^(?:Net )?profit attributable to ?:?$", re.I)  # then "Owners of the Company"
OWNERS_RE = re.compile(r"^Owners of the Company$", re.I)
EPS_RE = re.compile(r"^Basic(?: EPS)? \((?:\$|in \$ per share)\)(?: \(\d+\))?$", re.I)  # "Basic ($)", "Basic (in $ per share)", "Basic EPS ($) (4)"
SHARES_HEAD_RE = re.compile(r"^Weighted average equity shares used in computing earnings per equity share$", re.I)
SHARES_RE = re.compile(r"^Basic(?: \(in shares\))?$", re.I)  # only under SHARES_HEAD_RE
EQUITY_RE = re.compile(r"^Total equity attributable to equity holders of the company$", re.I)
NCI_RE = re.compile(r"^Non-controlling interests?$", re.I)
TOTAL_EQUITY_RE = re.compile(r"^Total equity$", re.I)
DECIMAL_POINT_TYPO_RE = re.compile(r"^\d{1,3}\.\d{3}$")
DASHES = ("–", "—", "-")


def text_of(html: bytes, limit: int | None = None) -> str:
    from bs4 import BeautifulSoup

    return " ".join(BeautifulSoup(html[:limit] if limit else html, "lxml").get_text(" ").split())


def _date(m) -> pd.Timestamp:
    """The date of a match of DATE (or of a dict with its month, day and year)."""
    g = m.groupdict() if isinstance(m, re.Match) else m
    return pd.Timestamp(year=int(g["year"]), month=MONTHS.index(g["month"].title()) + 1, day=int(g["day"]))


def _tables(html: bytes) -> list[list[list[str]]]:
    from bs4 import BeautifulSoup

    return [table_rows(t) for t in BeautifulSoup(html, "lxml").find_all("table")]


def _header(rows: list[list[str]]) -> tuple[list[pd.Timestamp], int, int] | None:
    """The end dates of the two "3 months ended" columns of an income statement (this quarter, the same quarter last year), its number of
    figure columns and of header rows; None if the table is not an income statement whose first columns are three months."""
    for n in range(min(3, len(rows))):
        if _numbers(rows[n]) and not all(YEAR_CELL_RE.match(c) for c in rows[n]):
            break  # a row of figures: the header is above it
    else:
        return None
    head = [[c for c in r if not IGNORED_HEADER_RE.match(c)] for r in rows[:n]]
    head = [r for r in head if r]
    if len(head) == 1 and all(COLUMN_RE.match(c) for c in head[0]):  # the press release
        columns = [COLUMN_RE.match(c) for c in head[0]]
        if len(columns) < 2 or not all((m.group("kind") or "").lower() in ("3", "three") for m in columns[:2]):
            return None
        return [_date(m) for m in columns[:2]], len(columns), n
    if len(head) != 2 or not all(GROUP_RE.match(c) for c in head[0]):
        return None
    first = GROUP_RE.match(head[0][0])
    if (first.group("kind") or "").lower() not in ("3", "three"):
        return None
    if all(YEAR_CELL_RE.match(c) for c in head[1]) and first.group("month"):  # "Three months ended December 31," over "2014", "2013"
        dates = [_date({"month": first.group("month"), "day": first.group("day"), "year": c}) for c in head[1][:2]]
    elif all(DATE_CELL_RE.match(c) for c in head[1]):
        dates = [_date(DATE_CELL_RE.match(c)) for c in head[1][:2]]
    else:
        return None
    return dates, len(head[1]), n


def _number(cell: str) -> float | None:
    return 0.0 if cell in DASHES else to_number(cell)


def _numbers(row: list[str]) -> list[float]:
    """The figures of a row after its label ("–" is zero); a note number ("2.13") before them is dropped by taking the last figures."""
    return [v for v in (_number(c) for c in row[1:]) if v is not None]


def _income_statement(tables) -> tuple[pd.Timestamp, dict[str, list[float]]]:
    """This quarter's end and its figures (this quarter, the same quarter last year) from the first income statement with "3 months ended"
    columns; the shares (millions) only where it has them (the statements, and the press release until 2020)."""
    for rows in tables:
        if not rows or (header := _header(rows)) is None:
            continue
        dates, width, skip = header
        found: dict[str, list[float]] = {}
        section = ""  # the last row without figures: "Profit attributable to:", "Weighted average equity shares ..."
        for row in rows[skip:]:
            label = clean(row[0])
            values = _numbers(row)
            if len(values) < width:
                section = label
                continue
            values = values[-width:][:2]
            if REVENUE_RE.match(label):
                found.setdefault("revenue", values)
            elif PROFIT_RE.match(label) or (OWNERS_HEAD_RE.match(section) and OWNERS_RE.match(label)):
                found.setdefault("profit", values)
            elif EPS_RE.match(label):
                found.setdefault("eps", values)
            elif SHARES_HEAD_RE.match(section) and SHARES_RE.match(label):
                found.setdefault("wa_shares", [v / 1e6 for v in values])
            section = ""
        missing = [k for k in ("revenue", "profit", "eps") if k not in found]
        if missing:
            raise ValueError(f"the income statement has no {', '.join(missing)} row")
        if dates[1] != dates[0] - pd.DateOffset(years=1):
            raise ValueError(f"the second column ({dates[1]:%Y-%m-%d}) is not the same quarter last year")
        return dates[0], found
    raise ValueError("no income statement with \"3 months ended\" columns")


def _equity(tables, end: pd.Timestamp) -> tuple[float, str]:
    """Equity attributable to the equity holders at the end of the quarter (the first column of the balance sheet, which must be dated so) and
    a note on it ("" normally).

    It must equal "Total equity" less "Non-controlling interests" (a missing row: none). The press release of March 31, 2018 writes "9.960"
    for 9,960 (its "Total equity" row says 9,960): a figure with a decimal point among amounts in millions is read without it only when that
    check then holds, and the note says so."""
    for rows in tables:
        dates = [m for c in rows[0] if (m := DATE_CELL_RE.match(c))] if rows else []
        if len(dates) != 2:
            continue
        found: dict[str, tuple[float, str]] = {}
        for row in rows[1:]:
            label = clean(row[0])
            values = _numbers(row)
            if len(values) < 2:
                continue
            for key, pattern in (("owners", EQUITY_RE), ("nci", NCI_RE), ("total", TOTAL_EQUITY_RE)):
                if pattern.match(label):
                    found.setdefault(key, (values[-2], row[-2]))
        if "owners" not in found:
            continue
        first = _date(dates[0])
        if first != end:
            raise ValueError(f"the balance sheet's first column ({first:%Y-%m-%d}) is not the end of the quarter ({end:%Y-%m-%d})")
        if "total" not in found:
            raise ValueError("the balance sheet has no \"Total equity\" row")
        owners, cell = found["owners"]
        rest = found["total"][0] - found.get("nci", (0.0, ""))[0]
        if abs(owners - rest) < 0.5:
            return owners, ""
        if DECIMAL_POINT_TYPO_RE.match(cell) and abs(float(cell.replace(".", "")) - rest) < 0.5:
            fixed = float(cell.replace(".", ""))
            return fixed, f"the press release writes {cell} for {fixed:,.0f} (total equity less non-controlling interests)"
        raise ValueError(f"equity attributable to equity holders {owners} is not total equity less non-controlling interests ({rest})")
    raise ValueError("no balance sheet with a \"Total equity attributable to equity holders of the company\" row")


def release_title(html: bytes) -> str | None:
    """"IFRS USD press release for the quarter ended September 30, 2025" if the exhibit is the IFRS USD press release; None otherwise."""
    text = EXHIBIT_LABEL_RE.sub("", text_of(html, 20_000), count=1)
    if not RELEASE_START_RE.search(text[:RELEASE_START_CHARS]):
        return None
    try:
        end, _ = _income_statement(_tables(html))
    except ValueError:
        return None
    return f"IFRS USD press release for the quarter ended {end:%B} {end.day}, {end.year}"


def statements_title(html: bytes) -> str | None:
    """"IFRS USD condensed consolidated financial statements for the quarter ended September 30, 2025" if the exhibit is the IFRS statements
    in US dollars (not those in Indian rupees, not the press release); None otherwise."""
    if RAW_SHARES_RE.search(html) is None:
        return None
    text = text_of(html[: RAW_SHARES_RE.search(html).end() + 20_000])
    if not STATEMENTS_RE.search(text) or not STATEMENTS_HEADING_RE.search(text):
        return None
    try:
        end, values = _income_statement(_tables(statements_excerpt(html)))
    except ValueError:
        return None
    if "wa_shares" not in values:
        return None
    return f"IFRS USD condensed consolidated financial statements for the quarter ended {end:%B} {end.day}, {end.year}"


def statements_excerpt(html: bytes) -> bytes:
    """The start of the statements up to the end of the table with the weighted average shares, closed with </body></html>."""
    label = RAW_SHARES_RE.search(html)
    end = RAW_TABLE_END_RE.search(html, label.end()) if label else None
    if end is None:
        raise ValueError("no table with the weighted average equity shares in the IFRS statements")
    return html[: end.end()] + b"\n</body></html>\n"


def parse_release(html: bytes) -> Parsed:
    """Revenue, net profit, basic EPS (this quarter, the same quarter last year) and equity (end of this quarter) from the IFRS USD press release."""
    tables = _tables(html)
    end, values = _income_statement(tables)
    equity, note = _equity(tables, end)
    values["equity"] = [equity]
    return Parsed(CURRENCY, values, pd.Period(end, freq="Q"), notes={"equity": note} if note else None)


def parse_statements(html: bytes) -> Parsed:
    """The same figures and the basic weighted average shares (millions; this quarter, the same quarter last year) from the IFRS USD statements."""
    tables = _tables(html)
    end, values = _income_statement(tables)
    if "wa_shares" not in values:
        raise ValueError("the income statement has no basic weighted average shares")
    equity, note = _equity(tables, end)
    values["equity"] = [equity]
    return Parsed(CURRENCY, values, pd.Period(end, freq="Q"), notes={"equity": note} if note else None)
