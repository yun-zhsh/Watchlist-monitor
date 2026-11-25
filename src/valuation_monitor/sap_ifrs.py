"""Extract earnings data from SAP's quarterly results documents in HTML furnished on Form 6-K (IFRS, millions of euros).

SAP furnishes one document a quarter as exhibit 99.1. Its kind changed over the years: an earnings press release ("SAP Announces Second Quarter
and First Half 2015 Results"), a quarterly statement ("SAP Q3 2016 Quarterly Statement", "Quarterly Statement Q3 2026") or a press release with
the quarterly statement attached ("SAP Announces Q2 2022 Results"). Whatever the kind, the document has the same IFRS primary financial statements,
and they are read from its text (some documents have the statements as HTML tables, a few only as text, so the tables are not used):

  income statement   "Consolidated Income Statements - Quarter" for this quarter and the same quarter last year, then the year-to-date one. It ends
                     with the basic and diluted EPS and a footnote that gives the basic weighted average number of shares of both quarters:
                     "For the three months ended September 30, 2026 and 2025, the weighted average number of shares was 1,150 million (diluted
                     1,157 million) and 1,164 million (diluted: 1,172 million), respectively (treasury stock excluded)."
                     That footnote fixes the quarter and its statement: the statement is the text before it, from its "Total revenue" row on.
                     Revenue is "Total revenue", net profit the last "Attributable to owners of parent" row (with discontinued operations,
                     2023 and 2024, the first one is the continuing operations only), EPS "Earnings per share, basic (in EUR)" (not the row "from
                     continuing operations"). Every quarter has its own three-months statement, the fourth quarter as well.
                     In 2023 and 2024 the non-IFRS EPS is that of the continuing operations (see below); those filings carry a note on it
                     (Parsed.notes), which every TTM that uses one of their quarters shows next to its formula.
  balance sheet      "Equity attributable to owners of parent" at the end of this quarter and at the end of last year, followed by the non-controlling interests.
  Non-IFRS EPS       the reconciliation of the non-IFRS figures has the row "Earnings per share, basic (in EUR)" with four figures: IFRS and non-IFRS of
                     this quarter, IFRS and non-IFRS of the same quarter last year; it is the row whose IFRS figures are those of the income statement.
  key facts          the multi-quarter table "Financial and Non-Financial Key Facts" (columns "Q1 2025 ... TY 2025 Q1 2026 ...") gives the previous
                     quarter's revenue and EPS (IFRS and non-IFRS) where it has them, as the third figure of a row. Two quarters depend on it:
                     the quarterly statements for the first quarter of 2020 and the fourth quarter of 2021 have their tables only as images, so
                     2020Q1 comes from the key facts of the 2020Q2 statement and 2021Q4 from those of the 2022Q1 statement (and a year later from
                     the prior-year columns). The key facts must agree with the statements.

Footnote marks between a label and its figures ("1)", "(1)", "*", "1, 2", note numbers such as "(9 )") are skipped. Negative figures are written with
an en dash ("-1,738"). Some documents split the first letter of a word from the rest ("T otal revenue"); it is joined again before reading.
The same value in two places must agree and a missing value is an error: nothing is guessed.
"""

from __future__ import annotations

import re

import pandas as pd

from valuation_monitor.parsed import Parsed

MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
AMOUNT = r"-?\d{1,3}(?:,\d{3})*"
DECIMAL = r"-?\d+\.\d{2}"
NOTES = r"(?:\s*(?:\(\s?\d{1,2}\s?\)|\d\)|\*+|\d(?:,\s?\d)*))*"  # footnote marks and note numbers between a label and its figures
CHANGE = r"(?:[<>]?-?\d+(?:\.\d+)?|N/?A|NM)"  # the change column after this year and last year: "15", "-5", ">100", "<-100", "N/A"
NEXT_LABEL = r"(?=\s+[A-Za-z(]|\s*$)"  # the row ends where the next label starts
QUARTER_SHARES_RE = re.compile(
    r"For the three months ended (?P<month>[A-Z][a-z]+) (?P<day>\d{1,2}), (?P<year>\d{4}) and (?P<prior>\d{4}), the weighted average number of shares was "
    rf"(?P<a>{AMOUNT}) million \(diluted:? {AMOUNT} million\) and (?P<b>{AMOUNT}) million"
)
REVENUE_RE = re.compile(rf"\bTotal revenue{NOTES}\s+(?P<a>{AMOUNT})\s+(?P<b>{AMOUNT})\s+{CHANGE}{NEXT_LABEL}")
OWNERS_RE = re.compile(rf"\b(?:Profit )?[Aa]ttributable to owners of parent{NOTES}\s+(?P<a>{AMOUNT})\s+(?P<b>{AMOUNT})\s+{CHANGE}{NEXT_LABEL}")
EPS_RE = re.compile(rf"\bEarnings per share, basic \(in € ?\){NOTES}\s+(?P<a>{DECIMAL})\s+(?P<b>{DECIMAL})\s+{CHANGE}{NEXT_LABEL}")
NON_IFRS_EPS_RE = re.compile(rf"\bEarnings per share, basic \(in € ?\){NOTES}\s+(?P<a>{DECIMAL})\s+(?P<n>{DECIMAL})\s+(?P<b>{DECIMAL})\s+(?P<nb>{DECIMAL})\b")
CONTINUING = r"\bEarnings per share, basic \(in € ?\)" + NOTES + r" from continuing operations" + NOTES
CONTINUING_EPS_RE = re.compile(rf"{CONTINUING}\s+(?P<a>{DECIMAL})\s+(?P<b>{DECIMAL})\s+{CHANGE}{NEXT_LABEL}")
NON_IFRS_CONTINUING_EPS_RE = re.compile(rf"{CONTINUING}\s+(?P<a>{DECIMAL})\s+(?P<n>{DECIMAL})\s+(?P<b>{DECIMAL})\s+(?P<nb>{DECIMAL})\b")
EQUITY_RE = re.compile(rf"\bEquity attributable to owners of parent{NOTES}\s+(?P<a>{AMOUNT})\s+(?P<b>{AMOUNT})\s+Non-controlling interests\b")
KEY_FACTS_RE = re.compile(r"€ millions, unless otherwise stated(?P<columns>(?: (?:Q[1-4]|TY) \d{4}){5,})")
KEY_FACTS_CHARS = 8000  # a part of the key facts table under one header
KEY_FACTS_ROWS = {
    "revenue": (r"Total revenue(?: \(IFRS\))?", AMOUNT),
    "eps": (r"Earnings per share, basic \(IFRS, in €\)", DECIMAL),
    "non_ifrs_eps": (r"Earnings per share, basic \(non-IFRS, in €\)", DECIMAL),
}
SPLIT_INITIAL_RE = re.compile(r"(?<![A-Za-z])([A-Z]) (?=[a-z]{2,})")  # "T otal revenue" -> "Total revenue"
CONTINUING_NOTE = "non-IFRS EPS from continuing operations (excludes the Qualtrics discontinued operations)"  # shown with every TTM that uses such a quarter
SPACED_MINUS_RE = re.compile(r"(?<= )- (?=\d)")  # the first quarter of 2015 writes "- 3,859" and "- 23"
STATEMENT_CHARS = 6000  # the income statement before the footnote is about 2,000 characters


def document_text(html: bytes) -> str:
    from bs4 import BeautifulSoup

    text = " ".join(BeautifulSoup(html, "lxml").get_text(" ").replace("\xa0", " ").split())
    text = text.replace("–", "-").replace("−", "-").replace("’", "'")
    text = SPACED_MINUS_RE.sub("-", text)
    return SPLIT_INITIAL_RE.sub(r"\1", text)


def number(token: str) -> float:
    return float(token.replace(",", ""))


def quarter_footnote(text: str) -> re.Match:
    m = QUARTER_SHARES_RE.search(text)
    if m is None:
        raise ValueError("Not found: the footnote of the quarter's income statement (\"For the three months ended ..., the weighted average number of shares was ...\")")
    if m.group("month") not in MONTHS or int(m.group("prior")) != int(m.group("year")) - 1:
        raise ValueError(f"Unexpected period in the footnote: {m.group(0)}")
    return m


def key_facts(text: str) -> dict[str, dict[pd.Period, float]]:
    """The quarterly figures of the key facts table (the full-year columns are left out): series -> quarter -> value."""
    out: dict[str, dict[pd.Period, float]] = {}
    headers = list(KEY_FACTS_RE.finditer(text))
    for header, after in zip(headers, headers[1:] + [None]):
        labels = header.group("columns").split()
        columns = [(labels[i], int(labels[i + 1])) for i in range(0, len(labels), 2)]
        section = text[header.end() : min(header.end() + KEY_FACTS_CHARS, after.start() if after else len(text))]
        for key, (label, value) in KEY_FACTS_ROWS.items():
            m = re.search(rf"\b{label}((?:\s+{value}){{{len(columns)}}})(?!\s*-?\d)", section)
            if m is None:
                continue
            for (kind, year), token in zip(columns, m.group(1).split()):
                if kind == "TY":
                    continue
                p = pd.Period(year=year, quarter=int(kind[1]), freq="Q")
                if out.setdefault(key, {}).setdefault(p, number(token)) != number(token):
                    raise ValueError(f"The key facts disagree on {key} of {p}")
    return out


def parse_sap_statement(html: bytes) -> Parsed:
    text = document_text(html)
    footnote = quarter_footnote(text)
    period = pd.Period(year=int(footnote.group("year")), quarter=(MONTHS[footnote.group("month")] - 1) // 3 + 1, freq="Q")
    if pd.Timestamp(year=period.year, month=MONTHS[footnote.group("month")], day=int(footnote.group("day"))) != period.end_time.normalize():
        raise ValueError(f"The quarter does not end on a quarter end: {footnote.group(0)}")
    start = text.rfind("Total revenue", max(0, footnote.start() - STATEMENT_CHARS), footnote.start())
    if start < 0:
        raise ValueError(f"Not found: the income statement of {period} (no Total revenue row before its footnote)")
    statement = text[start : footnote.start()]

    revenue = REVENUE_RE.match(statement)
    owners = list(OWNERS_RE.finditer(statement))
    eps = list(EPS_RE.finditer(statement))
    if revenue is None or not owners or len(eps) != 1:
        raise ValueError(f"Not found in the income statement of {period}: revenue {bool(revenue)}, owners' profit {len(owners)}, basic EPS rows {len(eps)} (one expected)")
    values = {
        "revenue": [number(revenue.group("a")), number(revenue.group("b"))],
        "profit": [number(owners[-1].group("a")), number(owners[-1].group("b"))],  # the last row: the profit including discontinued operations
        "eps": [number(eps[0].group("a")), number(eps[0].group("b"))],
        "wa_shares": [number(footnote.group("a")), number(footnote.group("b"))],
    }

    continuing = CONTINUING_EPS_RE.search(statement)  # 2023 and 2024: Qualtrics was sold in June 2023 (discontinued operations)
    reconciliation, ifrs = (NON_IFRS_CONTINUING_EPS_RE, [number(continuing.group("a")), number(continuing.group("b"))]) if continuing else (NON_IFRS_EPS_RE, values["eps"])
    non_ifrs = [m for m in reconciliation.finditer(text) if [number(m.group("a")), number(m.group("b"))] == ifrs]
    if not non_ifrs:
        raise ValueError(f"Not found: the non-IFRS basic EPS of {period} (the reconciliation row with the IFRS EPS {ifrs})")
    values["non_ifrs_eps"] = [number(non_ifrs[0].group("n")), number(non_ifrs[0].group("nb"))]
    notes = {"non_ifrs_eps": CONTINUING_NOTE} if continuing else {}

    equity = [[number(m.group("a")), number(m.group("b"))] for m in EQUITY_RE.finditer(text)]
    if not equity:
        raise ValueError("Not found: the balance sheet (equity attributable to owners of parent)")
    if any(e != equity[0] for e in equity):
        raise ValueError(f"The balance sheets in this document disagree: {equity}")
    values["equity"] = equity[0]

    facts = key_facts(text)
    for key, by_quarter in facts.items():
        if key in notes:
            continue  # the key facts give the total, not the continuing operations: they are not mixed in
        if period in by_quarter and by_quarter[period] != values[key][0]:
            raise ValueError(f"The key facts give {key} {by_quarter[period]} for {period}, the statements {values[key][0]}")
        if period - 1 in by_quarter:
            values[key].append(by_quarter[period - 1])  # the previous quarter, as the third figure
    return Parsed("EUR", values, period, True, notes or None)
