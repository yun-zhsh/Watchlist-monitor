"""Extract earnings data from TSMC's quarterly earnings releases and earnings conference slides (6-K, HTML; see providers/sec.py, TSMC_RELEASE).

Verified on every earnings 6-K since 2015. TSMC reports under Taiwan-IFRS in New Taiwan dollars; one ADR (the NYSE listing, TSM) is five common
shares, a ratio unchanged since the listing in 1997 and not printed in the release, so it is a constant here (ADR_SHARES).

  release  "TSMC Reports Third Quarter EPS of NT$29.43": one table "(Unit: NT$ million, except for EPS)" with the columns of this quarter, the same
           quarter last year and the previous quarter ("3Q26 Amount", "3Q25 Amount", YoY %, "2Q26 Amount", QoQ %). Rows: Net sales (revenue),
           Net income (attributable to the shareholders of the parent, as the slides call it) and EPS (NT$), which is the diluted EPS: the release
           says "diluted earnings per share of NT$29.43" and gives no basic EPS. Each EPS carries a footnote mark ("29.43 b") whose note gives the
           weighted average shares behind it ("b: Based on 25,932 million weighted average outstanding shares"; the slides call them diluted).
  slides   "Selected Items from Balance Sheets" (In NT$ billions), columns this quarter, the previous quarter and the same quarter last year:
           "Total Shareholders' Equity" is the total equity, including non-controlling interests (the slides do not show the parent's share).
           Some decks (the fourth quarter of 2015, the first quarter of 2019 and the third quarter of 2019 to the fourth quarter of 2022) have
           their pages as images only; they have no equity (parse_slides returns None).

EPS is converted to NT$ per ADR (x 5) and the shares to millions of ADR (/ 5) so that they match the ADR price; amounts are in millions of NT$.
The same value in two places must agree and a missing value is an error: nothing is guessed.
"""

from __future__ import annotations

import re

import pandas as pd

from valuation_monitor.parsed import Parsed
from valuation_monitor.press_release import clean, table_rows

ADR_SHARES = 5  # common shares per ADR (TSM)
CURRENCY = "TWD"
TITLE_RE = re.compile(r"\bTSMC Reports (?P<quarter>First|Second|Third|Fourth) Quarter EPS of NT\$ ?\d+\.\d\d", re.I)
QUARTER_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
UNIT_RE = re.compile(r"\(Unit: NT\$ ?million, except for EPS\)", re.I)
QUARTER_CELL_RE = re.compile(r"\b(?P<q>[1-4])Q(?P<yy>\d\d)\b")
AMOUNT_RE = re.compile(r"^\(?\d{1,3}(?:,\d{3})*\)?$")
EPS_CELL_RE = re.compile(r"^(?P<eps>\(?\d+\.\d\d\)?)(?: (?P<mark>[a-e]))?$")
MARK_RE = re.compile(r"^[a-e]$")
SPACE_IN_NUMBER_RE = re.compile(r"(?<=\d\.) (?=\d)")
# the text of some releases breaks numbers with stray spaces ("b : Based on 25,93 0 million ..." in the third quarter of 2015)
SHARES_NOTE_RE = re.compile(r"\b(?P<mark>[a-e]) ?: ?Based on (?P<shares>\d[\d, ]*\d) million weighted average outstanding shares", re.I)
DILUTED_RE = re.compile(r"\bdiluted earnings per share of NT\$ ?(?P<eps>\d+\.\d\d)", re.I)
ROWS = {"revenue": re.compile(r"^net sales$", re.I), "profit": re.compile(r"^net income$", re.I), "eps": re.compile(r"^EPS \(NT\$\)$", re.I)}

SLIDES_RE = re.compile(r"\bSelected Items from Balance Sheets\b", re.I)
# the text of some decks runs the cells together ("1,197.33 66.4%1,280.9574.2%1,066.6366.3%", the second quarter of 2016) or breaks words
# ("In NT$ b illions", 2023-2025); an amount always has two decimals and its share of the total one, so a run still splits one way only.
# The columns are this quarter, the previous quarter and the same quarter last year; some decks have no quarter labels over them in the
# text (the third quarter of 2017, the first quarter of 2018), so the quarter is that of the deck's title.
SLIDES_HEADER_RE = re.compile(r"Selected Items from Balance Sheets(?P<labels>.{0,60}?)\(In ?\$? ?NT\$? ?b ?illions\)", re.I)
DECK_TITLE_RE = re.compile(r"\b(?P<year>20\d\d) (?P<quarter>First|Second|Third|Fourth) Quarter Earnings Conference\b", re.I)
LABEL_RE = re.compile(r"(?P<q>[1-4])Q(?P<yy>\d\d)")  # run together in some decks: "1Q162Q15"
BILLIONS = r"\d{1,3}(?:,\d{3})*\.\d\d"
SHARE_OF_TOTAL = r" ?\d{1,3}\.\d ?% ?"
EQUITY_RE = re.compile(rf"Total Shareholders['’]? ?Equity ?(?P<a>{BILLIONS}){SHARE_OF_TOTAL}(?P<b>{BILLIONS}){SHARE_OF_TOTAL}(?P<c>{BILLIONS}){SHARE_OF_TOTAL}", re.I)
EQUITY_NOTE = "total shareholders' equity from the earnings conference slides, including non-controlling interests (the slides do not show the parent's share), in NT$ billions to two decimals"
EPS_NOTE = f"diluted EPS (the release discloses no basic EPS), NT$ per share x {ADR_SHARES} = NT$ per ADR"


def text_of(html: bytes) -> str:
    from bs4 import BeautifulSoup

    return clean(BeautifulSoup(html, "lxml").get_text(" "))


def quarter_of(label: str) -> pd.Period:
    m = QUARTER_CELL_RE.fullmatch(label)
    return pd.Period(year=2000 + int(m.group("yy")), quarter=int(m.group("q")), freq="Q")


def title(html: bytes) -> str | None:
    """The headline of an earnings release ("TSMC Reports Third Quarter EPS of NT$29.43"); None for any other document."""
    m = TITLE_RE.search(text_of(html)[:5000])
    return m.group(0) if m else None


def number(cell: str) -> float:
    value = float(cell.strip("()").replace(",", ""))
    return -value if cell.startswith("(") else value


def results_table(soup) -> list[list[str]]:
    """The rows of the consolidated results table: the table with the "Net sales" row, and the tables after it while its rows go on
    (the releases for the first two quarters of 2024 continue it in another table from "Income before tax" on, after empty ones)."""
    tables = [table_rows(t) for t in soup.find_all("table")]
    start = next((i for i, rows in enumerate(tables) if any(r and ROWS["revenue"].match(r[0]) for r in rows)), None)
    if start is None:
        raise ValueError("no results table (a \"Net sales\" row) in the release")
    rows = list(tables[start])
    for more in tables[start + 1 :]:
        if not more:  # an empty table between the parts (layout only)
            continue
        if not any(r and (ROWS["profit"].match(r[0]) or ROWS["eps"].match(r[0])) for r in more):
            break
        rows += more
    return rows


def columns(rows: list[list[str]]) -> list[pd.Period]:
    """The quarters of the amount columns, from the header rows: this quarter, the same quarter last year, the previous quarter."""
    for i, row in enumerate(rows):
        if ROWS["revenue"].match(row[0]):
            labels = [m.group(0) for r in rows[:i] for cell in r for m in QUARTER_CELL_RE.finditer(cell)]
            break
    else:
        raise ValueError("no header before the \"Net sales\" row")
    if len(labels) != 3:
        raise ValueError(f"expected three quarters in the header, got {labels}")
    periods = [quarter_of(x) for x in labels]
    if periods[1] != periods[0] - 4 or periods[2] != periods[0] - 1:
        raise ValueError(f"unexpected columns {labels}: expected this quarter, the same quarter last year and the previous quarter")
    return periods


def amounts(row: list[str]) -> list[float]:
    """The three amounts of a row (this quarter, last year, previous quarter); the percentage columns after the second and third are left out."""
    cells = [c for c in row[1:] if not MARK_RE.match(c)]
    if len(cells) < 4 or not all(AMOUNT_RE.match(cells[k]) for k in (0, 1, 3)):
        raise ValueError(f"unexpected row {row}")
    return [number(cells[k]) for k in (0, 1, 3)]


def eps_row(row: list[str]) -> tuple[list[float], list[str]]:
    """The three EPS of the row and the footnote mark of each; a mark may be a cell of its own ("3.05", "b")."""
    cells: list[list[str]] = []  # [value, mark]
    for cell in row[1:]:
        cell = SPACE_IN_NUMBER_RE.sub("", cell)  # "5. 18 b" (the second quarter of 2022)
        if MARK_RE.match(cell) and cells and not cells[-1][1]:
            cells[-1][1] = cell
        elif m := EPS_CELL_RE.match(cell):
            cells.append([m.group("eps"), m.group("mark") or ""])
        else:
            cells.append([cell, ""])
    if len(cells) < 4 or not all(EPS_CELL_RE.match(cells[k][0]) for k in (0, 1, 3)):
        raise ValueError(f"unexpected EPS row {row}")
    return [number(cells[k][0]) for k in (0, 1, 3)], [cells[k][1] for k in (0, 1, 3)]


def parse_release(html: bytes, slides: bytes | None = None) -> Parsed:
    """Revenue, net income, diluted EPS (NT$ per ADR) and the weighted average shares (millions of ADR) of this quarter, the same quarter last year
    and the previous quarter; and the total equity at the end of the quarter from the slides, when they have a text balance sheet: those of the
    same 6-K (`slides`), or in the release document itself (the first quarters of 2019 and 2020 combine release and slides in one document)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    text = clean(soup.get_text(" "))
    head = TITLE_RE.search(text[:5000])
    if not head:
        raise ValueError("not a TSMC earnings release (no headline \"TSMC Reports ... Quarter EPS of NT$...\")")
    if not UNIT_RE.search(text):
        raise ValueError("the unit note \"(Unit: NT$ million, except for EPS)\" is missing")
    rows = results_table(soup)
    periods = columns(rows)
    if periods[0].quarter != QUARTER_WORDS[head.group("quarter").lower()]:
        raise ValueError(f"the headline ({head.group(0)}) and the table ({periods[0]}) disagree on the quarter")
    found: dict[str, list[str]] = {}
    for row in rows:
        for key, regex in ROWS.items():
            if regex.match(row[0]):
                if key in found:
                    raise ValueError(f"two rows {row[0]!r} in the results table")
                found[key] = row
    missing = [k for k in ROWS if k not in found]
    if missing:
        raise ValueError(f"rows missing from the results table: {missing}")
    eps, marks = eps_row(found["eps"])
    diluted = DILUTED_RE.search(text)
    if not diluted or abs(float(diluted.group("eps")) - eps[0]) > 1e-9:
        raise ValueError(f"the text's diluted EPS ({diluted and diluted.group('eps')}) does not match the table ({eps[0]})")
    notes = {m.group("mark"): number(m.group("shares").replace(" ", "")) for m in SHARES_NOTE_RE.finditer(text)}
    if not all(mark in notes for mark in marks):
        raise ValueError(f"no weighted average shares for the EPS marks {marks} (notes: {notes})")
    values = {
        "revenue": amounts(found["revenue"]),
        "profit": amounts(found["profit"]),
        "eps": [round(e * ADR_SHARES, 6) for e in eps],
        "wa_shares": [round(notes[mark] / ADR_SHARES, 6) for mark in marks],
    }
    series_notes = {"eps": EPS_NOTE}
    balance = parse_slides(slides) if slides is not None else parse_slides(html, text)
    if balance is not None:
        if balance[0] != periods[0]:
            raise ValueError(f"the slides are for {balance[0]}, the release for {periods[0]}")
        values["equity"] = [balance[1]]
        series_notes["equity"] = EQUITY_NOTE
    return Parsed(CURRENCY, values, periods[0], True, series_notes)


def slides_title(html: bytes) -> str | None:
    """The title of an earnings conference deck: "2026 Third Quarter Earnings Conference"; None for any other document."""
    m = DECK_TITLE_RE.search(text_of(html)[:3000])
    return m.group(0) if m else None


def has_text_balance_sheet(html: bytes) -> bool:
    """Whether a deck has its balance sheet slide as text (not only as a page image)."""
    return SLIDES_RE.search(text_of(html)) is not None


def parse_slides(html: bytes, text: str | None = None) -> tuple[pd.Period, float] | None:
    """The quarter and the total shareholders' equity at its end (millions of NT$) from the balance sheet slide; None when the deck has no text
    balance sheet (its pages are images). The quarter is that of the deck's title; quarter labels over the columns, where the text has them,
    must agree with it."""
    text = text if text is not None else text_of(html)
    if not SLIDES_RE.search(text):
        return None
    deck, header, equity = DECK_TITLE_RE.search(text), SLIDES_HEADER_RE.search(text), EQUITY_RE.search(text)
    if not deck or not header or not equity:
        raise ValueError("the balance sheet slide has no deck title, no unit \"(In NT$ billions)\" or no \"Total Shareholders' Equity\" row")
    period = pd.Period(year=int(deck.group("year")), quarter=QUARTER_WORDS[deck.group("quarter").lower()], freq="Q")
    labels = [pd.Period(year=2000 + int(m.group("yy")), quarter=int(m.group("q")), freq="Q") for m in LABEL_RE.finditer(header.group("labels"))]
    if labels and labels != [period, period - 1, period - 4]:
        raise ValueError(f"the balance sheet columns {[str(x) for x in labels]} do not fit the deck's quarter {period}")
    return period, round(number(equity.group("a")) * 1000, 6)  # billions to millions


# The reviewed (or, for a full year, audited) consolidated financial statements TSMC furnishes on 6-K about four weeks after each release, read
# only for the quarters whose slides have no text balance sheet: "Taiwan Semiconductor Manufacturing Company Limited and Subsidiaries
# Consolidated Financial Statements for the Three Months Ended March 31, 2019 and 2018 and Independent Auditors' Review Report" (also "Six",
# "Nine" months, or "for the Years Ended December 31, 2015 and 2014"). The parent-company-only statements furnished next to the annual ones are
# not taken. Of a 2-12 MB document only an excerpt is saved: the original HTML from its start (the 6-K cover, the title, the auditors' report)
# up to the end of the table with the first "Total equity" row, the consolidated balance sheet (statements_excerpt).
STATEMENTS_TITLE_RE = re.compile(
    r"Taiwan Semiconductor Manufacturing Company Limited and Subsidiaries Consolidated Financial Statements? for the "
    r"(?:(?:Three|Six|Nine) Months|Years?) Ended (?P<date>(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4})",
    re.I,
)
STATEMENTS_TITLE_CHARS = 5000
BALANCE_HEADING_RE = re.compile(r"CONSOLIDATED BALANCE SHEETS", re.I)
DATE_RE = re.compile(r"(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}")
THOUSANDS = r"\(?\d{1,3}(?:,\d{3})+\)?"
TOTAL_EQUITY_RE = re.compile(rf"\bTotal equity \$? ?(?P<a>{THOUSANDS})", re.I)
RAW_TOTAL_EQUITY_RE = re.compile(rb"Total\s+equity\s*<", re.I)  # the label cell in the HTML
RAW_TABLE_END_RE = re.compile(rb"</table\s*>", re.I)
STATEMENTS_EQUITY_NOTE = "total equity from the reviewed consolidated financial statements, including non-controlling interests (as in the slides), in NT$ thousands"
STATEMENTS_NOTE = "consolidated financial statements"  # Filing.note: what the equity comes from (see build_history)


def statements_title(html: bytes) -> str | None:
    """The title of TSMC's consolidated financial statements; None for any other document (the parent-company-only statements included)."""
    m = STATEMENTS_TITLE_RE.search(text_of(html[:400_000])[:STATEMENTS_TITLE_CHARS])
    return m.group(0) if m else None


def statements_period(title_text: str) -> pd.Period:
    return pd.Timestamp(STATEMENTS_TITLE_RE.search(title_text).group("date")).to_period("Q")


def statements_excerpt(html: bytes) -> bytes:
    """The start of the original document up to the end of the consolidated balance sheet table, closed with </body></html>."""
    label = RAW_TOTAL_EQUITY_RE.search(html)
    end = RAW_TABLE_END_RE.search(html, label.end()) if label else None
    if end is None:
        raise ValueError("no table with a \"Total equity\" row in the consolidated financial statements")
    return html[: end.end()] + b"\n</body></html>\n"


def parse_statements(html: bytes) -> tuple[pd.Period, float]:
    """The quarter and the total equity at its end (millions of NT$) from the consolidated balance sheet (amounts in thousands); the first
    column of the balance sheet must be the end of the quarter of the title."""
    text = text_of(html)
    head = STATEMENTS_TITLE_RE.search(text[:STATEMENTS_TITLE_CHARS])
    if head is None:
        raise ValueError("not TSMC's consolidated financial statements (no title)")
    period = statements_period(head.group(0))
    heading = BALANCE_HEADING_RE.search(text, head.end())
    first_date = DATE_RE.search(text, heading.end()) if heading else None
    if first_date is None or pd.Timestamp(first_date.group(0)) != period.end_time.normalize():
        raise ValueError(f"the balance sheet's first column ({first_date and first_date.group(0)}) is not the end of {period}")
    equity = TOTAL_EQUITY_RE.search(text, heading.end())
    if equity is None:
        raise ValueError("no \"Total equity\" row in the consolidated balance sheet")
    return period, round(number(equity.group("a")) / 1000, 6)  # thousands to millions
