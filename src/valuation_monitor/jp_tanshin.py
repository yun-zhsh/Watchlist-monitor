"""Extract earnings data from the Kessan Tanshin (summary of consolidated financial results, Japanese GAAP) of Japanese companies
(PDF originals in Japanese; verified on all SHIFT Inc. filings from the first quarter of FY2015 on, including the corrected ones).

Every Kessan Tanshin starts with the same summary, which is what is read:
  (1) consolidated operating results, year to date (three, six or nine months, or the full year), this year and the same period
      last year: net sales and profit attributable to owners of parent (millions of yen)
  the per-share table under it: basic EPS (yen; older filings print yen and sen as two numbers, "7 73" for 7.73)
  (2) financial position: (reference) equity, the net assets less share acquisition rights and non-controlling interests, i.e. the
      equity attributable to owners of the parent (Japanese GAAP has no preference shares or perpetual bonds in it)
  the notes: shares issued at the end of the period (including treasury shares) less treasury shares at the end of the period
      (including those held by the stock-benefit ESOP trust, which are excluded from the EPS share count too)
The figures of the first quarter are the quarter itself; those of the second and third quarters and the full year are year to date
(quarterly=False), and the single quarters are their differences (see filings.build_history, which follows the fiscal year).
Only the equity at the end of this period is returned (not the earlier year end), and the shares are the period-end shares
outstanding (filings.PERIOD_END_SHARES).

The title is the English one built by the Xj-Storage provider: "FY2026 Q1 Kessan Tanshin (consolidated results for the three
months ended November 30, 2025)", "FY2025 Kessan Tanshin (consolidated results for the year ended August 31, 2025)", with
"Correction of the " in front for a corrected filing. A correction starts with a cover letter and then reprints the corrected
Kessan Tanshin in full; its summary is read the same way.

The Japanese patterns live in jp_patterns.py; this module is language-neutral.
"""

from __future__ import annotations

import io
import re
import unicodedata

import pandas as pd
from pypdf import PdfReader

from valuation_monitor import jp_patterns as jp
from valuation_monitor.parsed import Parsed

TITLE_RE = re.compile(
    r"^(?P<correction>Correction of the )?FY(?P<fy>\d{4}) (?:Q(?P<quarter>[1-3]) )?Kessan Tanshin \(consolidated results for the "
    r"(?P<span>three months|six months|nine months|year) ended (?P<end>[A-Z][a-z]+ \d{1,2}, \d{4})\)$"
)
SPANS = {"three months": 1, "six months": 2, "nine months": 3, "year": 4}
MINUS = f"[{jp.MINUS_SIGNS}]"
DASH = f"[{re.escape(jp.DASHES)}]"
NUM = rf"{MINUS}?\d[\d,]*(?:\.\d+)?"
CELL = rf"(?:{NUM}|{DASH})"
# EPS in yen: "6.82", or yen and sen as two numbers ("7 73"), or no figure
EPS_CELL = rf"(?:{MINUS}?\d[\d,]*\.\d+|{MINUS}?\d[\d,]* \d{{2}}(?![\d.,])|{DASH})"
RESULT_COLUMNS = 8  # net sales, operating profit, ordinary profit, profit attributable to owners of parent, each with its change in percent
REVENUE_COLUMN, PROFIT_COLUMN = 0, 6
SUMMARY_SEARCH = 6000  # characters after the operating results within which the rest of the summary is looked for
MILLION = 1e6


def english_title(fiscal_year: int, quarter: int, period_end: pd.Timestamp, correction: bool = False) -> str:
    """The title used everywhere in the program (quarter: the fiscal quarter, 4 for the full year)."""
    span = {1: "three months", 2: "six months", 3: "nine months", 4: "year"}[quarter]
    title = (
        f"FY{fiscal_year} {f'Q{quarter} ' if quarter < 4 else ''}Kessan Tanshin "
        f"(consolidated results for the {span} ended {period_end:%B} {period_end.day}, {period_end.year})"
    )
    return f"Correction of the {title}" if correction else title


def normalize(text: str) -> str:
    """NFKC (full-width digits, letters and brackets to ASCII), whitespace collapsed, spaces between Japanese characters removed."""
    text = " ".join(unicodedata.normalize("NFKC", text).split())
    return re.sub(jp.JP_GAP_RE, "", text)


def page_texts(pdf: bytes) -> list[str]:
    return [normalize(p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages]


def read_text(pdf: bytes) -> str:
    return " ".join(page_texts(pdf))


def has_summary(text: str) -> bool:
    """Whether a (normalized) text has the summary of operating results and the share counts (a correction of other items does not)."""
    start = text.find(jp.OPERATING_RESULTS)
    return start >= 0 and re.search(jp.TREASURY_SHARES, text[start:]) is not None


def summary_pages(texts: list[str]) -> list[int]:
    """The pages up to the one with the treasury share count after the operating results: the summary (and a correction's cover letter)."""
    results = next((i for i, t in enumerate(texts) if jp.OPERATING_RESULTS in t), None)
    if results is not None:
        for i in range(results, len(texts)):
            if re.search(jp.TREASURY_SHARES, texts[i]):
                return list(range(i + 1))
    raise ValueError("The summary of the Kessan Tanshin (operating results and share counts) was not found")


def to_number(token: str) -> float | None:
    """A cell as a number; None for a dash (no figure). Yen and sen written apart ("7 73") are joined."""
    token = token.strip()
    if re.fullmatch(DASH, token):
        return None
    negative = token[0] in jp.MINUS_SIGNS
    token = token.lstrip(jp.MINUS_SIGNS)
    if " " in token:
        yen, sen = token.split(" ")
        token = f"{yen}.{sen}"
    value = float(token.replace(",", ""))
    return -value if negative else value


def share_count(token: str) -> float:
    """A share count; a dash means none. Separators are dropped (one filing prints "66.260" for 66,260 shares)."""
    if re.fullmatch(DASH, token):
        return 0.0
    return float(re.sub(r"[,.]", "", token))


def _label_matches(m: re.Match, fiscal_year: int, quarter: int) -> bool:
    year = int(m.group("year"))
    if year not in (fiscal_year, fiscal_year - jp.HEISEI_OFFSET):
        return False
    if quarter == 4:
        return m.group("quarter") is None and m.group("interim") is None
    return int(m.group("quarter") or (2 if m.group("interim") else 0)) == quarter


def _row(
    text: str, name: str, start: int, end: int, cell: str, count: int, fiscal_year: int, quarter: int, first: bool = True
) -> tuple[list[float | None], int]:
    """The row in text[start:end] whose label is this fiscal year and quarter, followed by `count` cells: the first row of the table
    (this period comes first), or with first=False the first such row anywhere in the range."""
    pattern = re.compile(rf"{jp.ROW_LABEL}\s?" + r"\s".join([f"({cell})"] * count))
    for m in pattern.finditer(text, start, end):
        if _label_matches(m, fiscal_year, quarter):
            return [to_number(g) for g in m.groups()[4:]], m.end()
        if first:
            break
    raise ValueError(f"Row not found in the Kessan Tanshin: {name} (FY{fiscal_year}{f' Q{quarter}' if quarter < 4 else ''})")


def parse_jp_tanshin(title: str, pdf: bytes) -> Parsed:
    m = TITLE_RE.match(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    fiscal_year, quarter = int(m.group("fy")), int(m.group("quarter") or 4)
    if SPANS[m.group("span")] != quarter:
        raise ValueError(f"The title does not agree with itself: {title}")
    period = pd.Period(pd.Timestamp(m.group("end")), freq="Q")
    text = read_text(pdf)

    start = text.find(jp.OPERATING_RESULTS)
    if start < 0:
        raise ValueError("The consolidated operating results were not found")
    end = start + SUMMARY_SEARCH
    current, pos = _row(text, "operating results", start, end, CELL, RESULT_COLUMNS, fiscal_year, quarter)
    try:
        prior, _ = _row(text, "operating results (prior year)", pos, pos + 200, CELL, RESULT_COLUMNS, fiscal_year - 1, quarter)
    except ValueError:
        prior = [None] * RESULT_COLUMNS
    eps_start = text.find(jp.EPS_SECTION, pos)
    if eps_start < 0:
        raise ValueError("The per-share table was not found")
    eps, eps_pos = _row(text, "basic EPS", eps_start, end, EPS_CELL, 2, fiscal_year, quarter)
    try:
        prior_eps, _ = _row(text, "basic EPS (prior year)", eps_pos, eps_pos + 200, EPS_CELL, 2, fiscal_year - 1, quarter)
    except ValueError:
        prior_eps = [None, None]
    if eps[0] is None or current[REVENUE_COLUMN] is None or current[PROFIT_COLUMN] is None:
        raise ValueError("The Kessan Tanshin has no net sales, profit or basic EPS for this period")

    equity_at = re.compile(jp.EQUITY_LABEL).search(text, pos, end)
    if equity_at is None:
        raise ValueError("The (reference) equity was not found")
    equity = re.compile(rf"\s?{jp.ROW_LABEL}\s?({NUM})\s?{jp.MILLION_YEN}").match(text, equity_at.end())
    if equity is None or not _label_matches(equity, fiscal_year, quarter):
        raise ValueError("The (reference) equity of this period was not found")

    shares = {}
    for name, label in (("issued", jp.ISSUED_SHARES), ("treasury", jp.TREASURY_SHARES)):
        found = re.compile(rf"{label}\s?{jp.SHARE_ROW_LABEL}\s?(\d[\d,.]*|{DASH})\s?{jp.SHARES_UNIT}").search(text, pos)
        if found is None:
            raise ValueError(f"The {name} shares at the end of the period were not found")
        shares[name] = share_count(found.group(1))

    values = {
        "revenue": [current[REVENUE_COLUMN]] + ([prior[REVENUE_COLUMN]] if prior[REVENUE_COLUMN] is not None else []),
        "profit": [current[PROFIT_COLUMN]] + ([prior[PROFIT_COLUMN]] if prior[PROFIT_COLUMN] is not None else []),
        "eps": [eps[0]] + ([prior_eps[0]] if prior_eps[0] is not None else []),
        "equity": [to_number(equity.group(5))],
        "wa_shares": [(shares["issued"] - shares["treasury"]) / MILLION],  # period-end shares outstanding (millions)
    }
    return Parsed("JPY", values, period, quarterly=quarter == 1)
