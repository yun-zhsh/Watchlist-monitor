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
SHARES_NOTE_RE = re.compile(r"\b(?P<mark>[a-e]) ?: ?Based on (?P<shares>\d{1,3}(?:,\d{3})*) million weighted average outstanding shares", re.I)
DILUTED_RE = re.compile(r"\bdiluted earnings per share of NT\$ ?(?P<eps>\d+\.\d\d)", re.I)
ROWS = {"revenue": re.compile(r"^net sales$", re.I), "profit": re.compile(r"^net income$", re.I), "eps": re.compile(r"^EPS \(NT\$\)$", re.I)}

SLIDES_RE = re.compile(r"\bSelected Items from Balance Sheets\b", re.I)
SLIDES_HEADER_RE = re.compile(r"Selected Items from Balance Sheets (?:TSMC Property )?(?P<a>[1-4]Q\d\d) (?P<b>[1-4]Q\d\d) (?P<c>[1-4]Q\d\d) \(In NT\$ ?billions\)", re.I)
BILLIONS = r"\d{1,3}(?:,\d{3})*\.\d\d"
EQUITY_RE = re.compile(rf"Total Shareholders['’]? Equity (?P<a>{BILLIONS}) [\d.]+ ?% (?P<b>{BILLIONS}) [\d.]+ ?% (?P<c>{BILLIONS}) [\d.]+ ?%", re.I)
EQUITY_NOTE = "total shareholders' equity from the earnings conference slides, including non-controlling interests (the slides do not show the parent's share); in NT$ billions to two decimals"
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
    """The rows of the consolidated results table: the one with a "Net sales" row."""
    for table in soup.find_all("table"):
        rows = table_rows(table)
        if any(r and ROWS["revenue"].match(r[0]) for r in rows):
            return rows
    raise ValueError("no results table (a \"Net sales\" row) in the release")


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
    notes = {m.group("mark"): number(m.group("shares")) for m in SHARES_NOTE_RE.finditer(text)}
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
    m = re.search(r"\b20\d\d (?:First|Second|Third|Fourth) Quarter Earnings Conference\b", text_of(html)[:3000], re.I)
    return m.group(0) if m else None


def has_text_balance_sheet(html: bytes) -> bool:
    """Whether a deck has its balance sheet slide as text (not only as a page image)."""
    return SLIDES_RE.search(text_of(html)) is not None


def parse_slides(html: bytes, text: str | None = None) -> tuple[pd.Period, float] | None:
    """The quarter and the total shareholders' equity at its end (millions of NT$) from the balance sheet slide; None when the deck has no text
    balance sheet (its pages are images)."""
    text = text if text is not None else text_of(html)
    if not SLIDES_RE.search(text):
        return None
    header, equity = SLIDES_HEADER_RE.search(text), EQUITY_RE.search(text)
    if not header or not equity:
        raise ValueError("the balance sheet slide has no header of three quarters or no \"Total Shareholders' Equity\" row")
    a, b, c = (quarter_of(header.group(k)) for k in "abc")
    if b != a - 1 or c != a - 4:
        raise ValueError(f"unexpected balance sheet columns {header.group('a', 'b', 'c')}")
    return a, round(number(equity.group("a")) * 1000, 6)  # billions to millions
