"""Extract earnings data from SK Hynix's English consolidated financial statements (K-IFRS) and earnings presentations.

Layout skhynix_statements. Two kinds of document, both from the company IR website (see providers/skhynix.py):

  report  The English review report (first three quarters) or audit report (full year) of the consolidated financial statements, as an excerpt of the
          pages the parser reads (statement_pages): the statements of financial position, the statements of comprehensive income and the earnings per
          share note.
            - Statement of comprehensive income: the first quarter and the full year have two columns (this period, same period last year); the second
              and third quarters have four (three months this year, year to date, three months last year, year to date), of which the two three-month
              columns are used. The full year is cumulative; the program obtains the fourth quarter as full year minus the first three quarters.
            - Revenue ("Revenue", "Net sales" or "Sales"), profit attributable to owners of the Parent Company, basic EPS (KRW); amounts in millions of KRW.
            - Equity attributable to owners of the Parent Company = Total equity - Non-controlling interests, at the end of this period and of last year.
            - Earnings per share note: weighted average number of ordinary shares outstanding (in shares), same columns as the income statement.
  deck    The earnings presentation, published at the earnings call (the reports come out weeks later), as an excerpt of the pages read (deck_pages).
          It is used for the latest quarter until its report is on the website (provisional, replaced afterwards), and for the quarters whose
          report on the website is a scanned image without a text layer. Its figures are in billions of KRW (rounded); net profit and shareholders'
          equity include non-controlling interests (small: about 0.1% of equity), which is noted with every figure taken from it (DECK_NOTES).
          Only the quarter's own figures are read (not the comparison columns). The fourth quarter's presentation gives the quarter, not the full year:
          its quarterly figures are used as they are.
SK Hynix has ordinary shares only (no preferred shares).
"""

from __future__ import annotations

import io
import re

import pandas as pd
from pypdf import PdfReader

from valuation_monitor.parsed import Parsed

PERIOD_END = {1: "March 31", 2: "June 30", 3: "September 30", 4: "December 31"}
TITLE_RE = re.compile(r"(?P<year>20\d\d) Q(?P<q>[1-4])\b")

AMOUNT = r"\(?\d{1,3}(?:,\d{3})*\)?|-"  # an amount or EPS, negative in parentheses; "-" is zero
NOTE_REF = re.compile(r"\d{1,2}(?:,\d{1,2})*")  # note references such as "4,21,28" or "32" in front of the amounts
TOKEN = re.compile(r"\s*(W|\\|\(?[\d,]+\)?|-(?=\s|$))")


def page_texts(pdf: bytes) -> list[str]:
    return [clean_text(p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages]


def clean_text(text: str) -> str:
    return " ".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text).split())


def to_number(token: str) -> float:
    if token == "-":
        return 0.0
    value = float(token.strip("()").replace(",", ""))
    return -value if token.startswith("(") else value


def row(text: str, label: str, columns: int, name: str) -> list[float]:
    """The numbers of the first row whose label matches `label` (a regex): the `columns` numbers after it, skipping the currency sign (W) and a
    leading note reference."""
    m = re.search(label, text)
    if not m:
        raise ValueError(f"Not found in the statements: {name}")
    tokens, pos = [], m.end()
    while t := TOKEN.match(text, pos):
        pos = t.end()
        if t.group(1) not in ("W", "\\"):
            tokens.append(t.group(1))
    if len(tokens) > columns and NOTE_REF.fullmatch(tokens[0]):
        tokens = tokens[1:]
    amounts = [t for t in tokens[:columns] if re.fullmatch(AMOUNT, t)]
    if len(amounts) < columns:
        raise ValueError(f"{name}: expected {columns} numbers after the label, found {tokens[:columns]}")
    return [to_number(t) for t in amounts]


def heading(text: str) -> str:
    return text[:250]


NOT_A_STATEMENT = re.compile(r"(?i)changes in equity|cash flows|page\(s\)|^contents|^index|notes to")  # the table of contents and the notes name the statements too


def is_income_statement(text: str) -> bool:
    return bool(re.search(r"(?i)statements? of (?:comprehensive )?income", heading(text))) and not NOT_A_STATEMENT.search(heading(text))


def is_balance_sheet(text: str) -> bool:
    return bool(re.search(r"(?i)statements? of financial position", heading(text))) and not NOT_A_STATEMENT.search(heading(text))


SHARES_RE = re.compile(
    r"Weighted[- ]average number of (?:outstanding )?(?:ordinary|common) shares(?: outstanding)?(?:1|\(\*\))?"
)


def is_eps_note(text: str) -> bool:
    """The page of the earnings per share note with the table of weighted average shares (the first row with numbers)."""
    return bool(re.search(SHARES_RE.pattern + r" (?:W )?\d{1,3}(?:,\d{3})+", text)) and bool(re.search(r"(?i)notes to", heading(text)))


def statement_pages(texts: list[str]) -> list[int]:
    """The pages of a report the parser reads: statements of financial position and of comprehensive income (with their continued pages) and the
    first page of the earnings per share note with the weighted average shares."""
    pages = [i for i, t in enumerate(texts) if is_balance_sheet(t) or is_income_statement(t)]
    note = next((i for i, t in enumerate(texts) if i > max(pages, default=0) and is_eps_note(t)), None)
    return pages + ([note] if note is not None else [])


def period_of(title: str) -> tuple[int, int]:
    m = TITLE_RE.search(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    return int(m.group("year")), int(m.group("q"))


REVENUE = r"(?<![\w-])(?:Revenue|Net sales|Sales)(?: \(note \d+\))?"
PROFIT = r"(?:Profit|Net income|Net profit)(?: \(loss\))?(?: for the (?:year|period))?(?: \(loss\))? attributable to:? (?:Owners|owners|Equity holders|equity holders) of the (?:Parent|parent)(?: Company| company)?"
BASIC_EPS = r"Basic (?:and diluted )?earnings ?(?:\((?:loss|losses)\) )?per share(?: \(in (?:Korean )?won\))?"
TOTAL_EQUITY = r"(?<!liabilities and )(?<!Liabilities and )Total (?:equity|shareholders' equity)(?! attributable)"
NCI = r"Non-controlling interests?"


def parse_report(title: str, pdf: bytes) -> Parsed:
    return parse_report_texts(title, page_texts(pdf))


def parse_report_texts(title: str, texts: list[str]) -> Parsed:
    year, quarter = period_of(title)
    income = " ".join(t for t in texts if is_income_statement(t))
    balance = " ".join(t for t in texts if is_balance_sheet(t))
    if not income or not balance:
        raise ValueError("The statements of comprehensive income and of financial position were not found (scanned or unexpected layout)")
    if not re.search(r"(?i)in millions of (?:korean )?won", income):
        raise ValueError("The income statement is not in millions of KRW")
    if PERIOD_END[quarter] not in income.replace(" ,", ","):
        raise ValueError(f"The income statement does not mention {PERIOD_END[quarter]}, so it does not match {year} Q{quarter} in the title")
    columns = 4 if quarter in (2, 3) else 2
    pick = (lambda v: [v[0], v[2]]) if columns == 4 else (lambda v: v)
    total, nci = row(balance, TOTAL_EQUITY, 2, "Total equity"), row(balance, NCI, 2, "Non-controlling interests")
    values = {
        "revenue": pick(row(income, REVENUE, columns, "Revenue")),
        "profit": pick(row(income, PROFIT, columns, "Profit attributable to owners of the Parent Company")),
        "eps": pick(row(income, BASIC_EPS, columns, "Basic earnings per share")),
        "equity": [t - n for t, n in zip(total, nci)],
    }
    note = " ".join(t for t in texts if is_eps_note(t))
    if note:
        values["wa_shares"] = [v / 1e6 for v in pick(row(note, SHARES_RE.pattern, columns, "Weighted average number of ordinary shares"))]
    return Parsed("KRW", values, pd.Period(year=year, quarter=quarter, freq="Q"), quarterly=quarter != 4)


# Earnings presentation: the income statement summary ("Results of Operation" or "Income Statement") and the capital structure page, in billions of KRW.
# The columns are named in a header right before the first row, e.g. "Q1’15 QoQ Q4‘14 Q1’14 YoY" (2014-2017: a percentage after this quarter and after the
# same quarter last year) or "’25 Q1 ’24 Q4 ’24 Q1 Q/Q Y/Y" (since 2018: the three quarters, then the percentages). The columns of this quarter, the previous
# quarter and the same quarter last year are found by their names; a percentage split in two tokens ("+11 %") is joined first.
QUOTE = "[’'‘`]"
COLUMN_RE = re.compile(rf"vs\. ?(?:Q[1-4] ?{QUOTE}\d\d|{QUOTE}\d\d ?Q[1-4])|(?P<quarter>Q[1-4] ?{QUOTE}\d\d|{QUOTE}\d\d ?Q[1-4])\**|QoQ|YoY|Q/Q|Y/Y")
HEADER_RE = re.compile(rf"(?:(?:{COLUMN_RE.pattern}) ?){{3,}}(?=(?:Revenue|Sales) )")
DECK_EPS_ROW = r"EPS ?\((?:Won|KRW)\)(?: ?Basic)?"
DECK_ROWS = {
    "revenue": r"(?<![\w-])(?:Revenue|Sales)",
    "profit": r"Net (?:Profit|Income)(?: \(Loss\))?",
    "eps": DECK_EPS_ROW,
    "wa_shares": r"Shares Outstanding ?\(M ?il\. ?[Ss]hares\)(?: ?Basic)?",
}
DECK_EQUITY = r"Shareholders[’'`]? ?Equity(?= [\d(])"
DECK_NUMBER = re.compile(r"\(?[-△]?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?")
BILLION = 1_000  # millions of KRW
DECK_NOTES = {
    "revenue": "from the earnings presentation (in billions of KRW, rounded)",
    "profit": "from the earnings presentation: net profit including non-controlling interests, in billions of KRW (rounded)",
    "equity": "from the earnings presentation: shareholders' equity including non-controlling interests, in billions of KRW (rounded)",
    "eps": "from the earnings presentation",
}


def deck_number(token: str, name: str) -> float:
    if not DECK_NUMBER.fullmatch(token):
        raise ValueError(f"{name}: {token!r} is not a number in the earnings presentation (the layout may have changed)")
    negative = token[0] in "-△"  # △ is the minus sign of Korean documents
    value = float(token.lstrip("-△").strip("()").replace(",", ""))
    return -value if negative or token.startswith("(") else value


def is_percentage(cell: str) -> bool:
    """A percentage or difference cell of a row: "+6%", "(44%)", "-5%p", "N/A", "-", "+1,089"."""
    return "%" in cell or cell in ("-", "N/A") or cell.startswith("+")


def deck_row(text: str, label: str, name: str, count: int) -> list[float]:
    """The first `count` numbers of the row after `label`, skipping the percentage and difference cells (a percentage split as "+11 %" is
    joined first); the row ends at the next word."""
    m = re.search(label, text)
    if not m:
        raise ValueError(f"{name} was not found in the earnings presentation (the layout may have changed)")
    cells: list[str] = []
    for token in text[m.end():].split():
        if token in ("%", "%p") and cells:
            cells[-1] += token
        elif re.search(r"[A-Za-z]", token) and token != "N/A":
            break
        else:
            cells.append(token)
    numbers = [c for c in cells if not is_percentage(c)][:count]
    if len(numbers) < count:
        raise ValueError(f"{name}: expected {count} numbers in the earnings presentation, found {cells}")
    return [deck_number(c, name) for c in numbers]


def is_deck_income(text: str) -> bool:
    return bool(re.search(DECK_EPS_ROW + r" ?[-\d(]", text)) and "KRW Billion" in text


def is_deck_capital(text: str) -> bool:
    return bool(re.search(DECK_EQUITY, text))


def deck_pages(texts: list[str]) -> list[int]:
    """The pages of a presentation the parser reads: the first income statement summary and the first capital structure page with shareholders' equity."""
    income = next((i for i, t in enumerate(texts) if is_deck_income(t)), None)
    if income is None:
        raise ValueError("The income statement summary with EPS was not found in the earnings presentation")
    capital = next((i for i, t in enumerate(texts) if is_deck_capital(t)), None)
    return sorted({income} | ({capital} if capital is not None else set()))


def quarter_name(label: str) -> tuple[int, int]:
    """(year, quarter) of a column name such as Q1’15 or ’23 Q1."""
    q, yy = re.search(r"Q([1-4])", label).group(1), re.search(r"\d\d(?! ?Q)", label.replace("Q1", "").replace("Q2", "").replace("Q3", "").replace("Q4", "")).group(0)
    return 2000 + int(yy), int(q)


def parse_deck(title: str, pdf: bytes) -> Parsed:
    return parse_deck_texts(title, page_texts(pdf))


def parse_deck_texts(title: str, texts: list[str]) -> Parsed:
    """The quarter's results in the earnings presentation (see the module docstring): revenue, net profit, basic EPS and shares of this quarter, and
    shareholders' equity at its end when the presentation gives it.

    Only this quarter's figures are taken. The columns of the previous quarter and of the same quarter last year are checked to be there (they tell the
    layout is the expected one) but not read: those quarters have reports with exact figures, and the rounded figures of the presentation (net profit
    including non-controlling interests; quarterly share counts where the full-year report gives the full year's) would only differ from them."""
    year, quarter = period_of(title)
    income = next((t for t in texts if is_deck_income(t)), None)
    if income is None:
        raise ValueError("The income statement summary with EPS was not found in the earnings presentation")
    header = HEADER_RE.search(income)
    if not header:
        raise ValueError("The column names of the income statement summary were not found in the earnings presentation (the layout may have changed)")
    columns = list(COLUMN_RE.finditer(header.group(0)))
    positions = {quarter_name(c.group("quarter")) for c in columns if c.group("quarter")}
    this = pd.Period(year=year, quarter=quarter, freq="Q")
    missing = [p for p in (this, this - 4, this - 1) if (p.year, p.quarter) not in positions]
    if missing or columns[0].group("quarter") is None or quarter_name(columns[0].group("quarter")) != (year, quarter):
        raise ValueError(f"The earnings presentation is not for {year} Q{quarter} or lacks the columns {[str(p) for p in missing]} (the layout may have changed)")
    body = income[header.end():]
    order = [quarter_name(c.group("quarter")) for c in columns if c.group("quarter")]  # the quarters of the value columns, in their order
    values = {}
    for series, label in DECK_ROWS.items():
        values[series] = [dict(zip(order, deck_row(body, label, series, len(order))))[(year, quarter)]]
    for series in ("revenue", "profit"):
        values[series] = [v * BILLION for v in values[series]]
    notes = {k: v for k, v in DECK_NOTES.items() if k != "equity"}
    capital = next((t for t in texts if is_deck_capital(t)), None)
    if capital is not None:
        values["equity"] = [deck_row(capital, DECK_EQUITY, "Shareholders' equity", 1)[0] * BILLION]
        notes["equity"] = DECK_NOTES["equity"]
    return Parsed("KRW", values, this, quarterly=True, notes=notes)
