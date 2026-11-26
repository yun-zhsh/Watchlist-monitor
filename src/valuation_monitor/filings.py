"""Extract earnings data from the original earnings filings and arrange it into per-period (quarter or half year) and TTM data.

Extracted data: revenue, net profit attributable to owners (millions), basic EPS (per share), Non-IFRS/Non-GAAP basic EPS (some companies have none),
equity attributable to owners (millions), shares disclosed in the filing (millions). The filing layout differs by company, so different parse functions handle them:

  highlights (Tencent, PDF, verified on all filings since 2014)
    - Financial highlights table on page 1: revenue, net profit attributable to owners, basic EPS, Non-IFRS basic EPS.
        Quarterly format: three numbers per row: this quarter, same quarter last year, previous quarter;
        cumulative format (interim, Q3 and annual filings before mid-2016): two numbers: this year to date,
        same period last year to date.
    - Balance sheet: equity attributable to owners, for the end of this period and the end of last year. EPS note: weighted average shares.
  statements (Sunny Optical, PDF, verified on all interim and annual filings since 2014)
    - Consolidated income statement: revenue, net profit attributable to owners, basic EPS (unit "RMB cents"), each with two numbers:
      this period to date, same period last year to date; amounts are in thousands and converted to millions when read.
    - Balance sheet: same as above. EPS note: weighted average shares.
  press_release (JD, Alibaba; HTML) is in press_release.py; us_press_release and us_gaap_statements (Meta, Cognizant, Dynatrace, Sea, ASML) in us_release.py;
  ifrs_statements (XP, HTML) in ifrs.py; sap_ifrs (SAP, HTML) in sap_ifrs.py; kifrs_statements (Samsung, PDF) in kifrs.py;
  cn_report (Hikvision, Glodon, PDF) in cn_report.py; tesla_update (Tesla's Update decks, and the Form 10-Q or 10-K of a quarter whose deck has
  its statements as page images only) in tesla_update.py. These parse functions return the common Parsed (see parsed.py).

A company may publish results every quarter (4 periods a year) or only interim and annual (2 periods a year; the second half is obtained as full year minus first half).
The number of periods is inferred from the existing filings, and the TTM is the sum of the periods in the last year (4 quarters or 2 half years).

The same value appears in several filings (this period, the next period's sequential comparison, next year's year-over-year comparison, the balance sheet's end of last year),
and the program compares them one by one; on disagreement the first published value is used and a notice is given.

Shares: the number disclosed in the filing is used (the basic EPS weighted average shares; period-end total shares for A-shares); where Q4 only discloses the full year (Sea's Q2 and Q3 only the year to date) it is derived by day-weighting;
only if none is disclosed is it implied as "net profit attributable to owners / basic EPS" of the period. The data available after each filing and how it is calculated are in _ttm_row and audit.py.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, replace

import pandas as pd
from pypdf import PdfReader

from valuation_monitor.market_calendar import HK, Market, effective_trading_day, get_market
from valuation_monitor.cn_report import parse_cn_express, parse_cn_report
from valuation_monitor.audit import Component, num, shares_formula, split_notes, sum_formula
from valuation_monitor.ifrs import parse_ifrs_statements
from valuation_monitor.kifrs import parse_kifrs, parse_kifrs_deck
from valuation_monitor.press_release import parse_press_release
from valuation_monitor.release_eps import parse_adjusted_eps_release
from valuation_monitor.sap_ifrs import parse_sap_statement
from valuation_monitor.tesla_update import parse_tesla_filing
from valuation_monitor.us_release import parse_us_release
from valuation_monitor.providers.base import Filing

MONTH_TO_QUARTER = {"MARCH": 1, "JUNE": 2, "SEPTEMBER": 3, "DECEMBER": 4}
PERIOD_RE = re.compile(r"ENDED (\d{1,2}) (MARCH|JUNE|SEPTEMBER|DECEMBER) (\d{4})", re.I)
EPS_RE = re.compile(
    r"(?P<non_ifrs>Non-IFRS |Non-GAAP )?EPS[\s()\d]*\((?P<ccy>[A-Z]{3}) per share\)[\s*]*\S?[–-] ?basic (?P<body>.*?)\S?[–-] ?diluted"
)
NUMBER_RE = re.compile(r"\(?\d+\.\d+\)?")
# Labels of the amount rows in the highlights table: only count when followed by a number (possibly with a footnote marker), to avoid matching the same words in the body text
REVENUE_RE = re.compile(r"\bRevenues? (?=[(*\d])")
PROFIT_RE = re.compile(r"(?<!IFRS )(?<!GAAP )Profit attributable to (?:the Company.s )?equity holders of the Company (?=[(*\d])")
EQUITY_RE = re.compile(
    r"EQUITY Equity attributable to (?:the Company.s )?equity holders(?: of the Company)?(?P<body>.*?)Non-controlling interests"
)
AMOUNT_RE = re.compile(r"\(?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?")
FOOTNOTE_RE = re.compile(r"\(\d\)|\*+")
CURRENCY_CODES = {"RMB": "CNY", "HK$": "HKD", "HKD": "HKD", "US$": "USD", "USD": "USD"}  # spelling in the filing -> ISO currency code

# Tencent: weighted average shares row in the EPS note (millions), two numbers each for three months and cumulative: this period, same period last year
WA_SHARES_RE = re.compile(
    r"Weighted average number of ordinary shares in issue(?: excluding [^()]*?)?(?:\s*\([^)]*\))*\s*((?:[\d,]+(?:\.\d+)?\s+){1,4})(?=[A-Z])", re.I
)
# Sunny: weighted average shares row in the EPS note (thousands), this period to date, same period last year to date
WA_SHARES_STMT_RE = re.compile(
    r"Weighted average number of ordinary shares for the purpose of basic earnings per share(?: \(Note\))? (?P<a>[\d,]+) (?P<b>[\d,]+)", re.I
)

# statements layout: rows in the consolidated income statement and balance sheet
AMT = r"\(?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?(?![\d,])"  # amounts with thousands separators; note numbers such as "3," do not count
NOTE = r"\d{1,2}[A-Za-z]?(?:, ?\d{1,2}[A-Za-z]?)*"  # note numbers: 3, 3A, 3, 4, 3,4
OWNERS = r"(?:owners|equity holders|shareholders) of the Company"
REVENUE_STMT_RE = re.compile(rf"\bRevenue (?:{NOTE} )?(?P<a>{AMT}) (?P<b>{AMT})")
PROFIT_STMT_RE = re.compile(rf"attributable to:? {OWNERS} (?P<a>{AMT}) (?P<b>{AMT})", re.I)
EQUITY_STMT_RE = re.compile(rf"Equity attributable to {OWNERS} (?P<a>{AMT}) (?P<b>{AMT})", re.I)
EPS_STMT_RE = re.compile(
    rf"Earnings per share\s*[–-]\s*Basic \((?P<ccy>RMB|HK\$|HKD|US\$|USD)(?P<cents> cents)?\) (?:{NOTE} )?(?P<a>\(?-?[\d.]+\)?) (?P<b>\(?-?[\d.]+\)?)",
    re.I,
)
# Labels before the net profit row. The total comprehensive income row is written exactly like the net profit row, so they can only be told apart by the label
PROFIT_LABEL_RE = re.compile(r"\bprofit(?: \(loss\))? for the (?:period|year)\s*$", re.I)
UNIT_RE = re.compile(r"RMB\W{0,2}(?P<unit>000|million)", re.I)  # amount unit in the header: RMB’000 or RMB million

PER_SHARE = ("eps", "non_ifrs_eps")  # per-share data, which must be adjusted for splits
AMOUNTS = ("revenue", "profit")  # amounts (millions)
FLOWS = PER_SHARE + AMOUNTS  # period data: can be split into quarters and summed into the TTM
BALANCES = ("equity",)  # point-in-time data (millions)
SHARE_COUNTS = ("wa_shares",)  # weighted average shares used for basic EPS as disclosed in the filing (millions; millions of ADS for ADRs), multiplied by the split ratio on a split
SERIES = FLOWS + BALANCES + SHARE_COUNTS
SERIES_LABELS = {
    "eps": "EPS",
    "non_ifrs_eps": "Non-IFRS EPS",
    "revenue": "Revenue",
    "profit": "Net profit attributable to owners",
    "equity": "Equity attributable to owners",
    "wa_shares": "Weighted average shares",
}
PARSER_VERSIONS = {"highlights": 5, "statements": 3, "press_release": 4, "us_press_release": 5, "us_gaap_statements": 4, "ifrs_statements": 1, "sap_ifrs": 2, "kifrs_statements": 4, "cn_report": 8, "tesla_update": 1}  # add 1 when the parsing rules change; saved parse results are then re-parsed automatically
SUPPLEMENT_FORMAT = "adjusted_eps_release"  # the layout of a supplementary document (XP's press release, see release_eps.py); its parse results carry their own version
SUPPLEMENT_PARSER_VERSION = 1  # add 1 when the rules of release_eps.py change
SUPPLEMENT_TABLE = "supplement"  # the "table" of a supplementary document's parse result in the stored announcements
PER_SHARE_TOLERANCE = 0.005  # allowed difference of the same value between filings: per-share data (rounding, subtraction of cumulative figures)
PER_SHARE_RELATIVE_TOLERANCE = 5e-4
AMOUNT_TOLERANCE = 0.001  # amounts: relative difference
SHARES_CHANGE_LIMIT = 0.05  # notice when the implied shares change by more than 5% between adjacent quarters (possible extraction error)

# Column order of the downloaded TTM file: filing information first, then the raw data of each period -> TTM total -> formula -> per-share data
TTM_FRAME_ORDER = [
    "report_date", "published", "available_date", "effective_date", "provisional",
    *[f"period_{k}" for k in range(1, 5)],
    *[f"eps_{k}" for k in range(1, 5)], "eps_ttm", "eps_ttm_formula",
    *[f"non_ifrs_eps_{k}" for k in range(1, 5)], "non_ifrs_eps_ttm", "non_ifrs_eps_ttm_formula",
    *[f"revenue_{k}" for k in range(1, 5)], "revenue_ttm", "revenue_ttm_formula",
    "profit_latest", "eps_latest", "shares", "shares_source", "shares_implied", "shares_formula",
    "revenue_per_share_ttm", "revenue_per_share_formula",
    "equity", "book_value_per_share", "book_value_per_share_formula", "split_note", "published_note",
]
TTM_COLUMNS = [
    "report_date", "published", "available_date",
    "eps_ttm", "non_ifrs_eps_ttm", "revenue_ttm", "shares",
    "revenue_per_share_ttm", "equity", "book_value_per_share",
]


PROVISIONAL_NOTES = {
    "KR": "comes from the earnings call presentation (before the external review; revenue and net profit are only precise to KRW 0.1 trillion and equity is an estimate); it is replaced automatically when the English financial statements are out",
    "CN": "comes from the express report (not audited by the accounting firm, so it may differ slightly from the final annual report numbers); it is replaced automatically when the annual report is out",
    "*": "is preliminary and is replaced automatically when the official report is out",
}


def tolerance(series: str, value: float) -> float:
    if series in PER_SHARE:  # per-share data is rounded to the cent or the unit, e.g. Samsung's EPS is a whole KRW figure and a split adjustment leaves a fraction
        return max(PER_SHARE_TOLERANCE, abs(value) * PER_SHARE_RELATIVE_TOLERANCE)
    return max(1.0, abs(value) * AMOUNT_TOLERANCE)


@dataclass
class Announcement:
    period: pd.Period  # reporting quarter of this filing, e.g. 2026Q2
    published: pd.Timestamp  # publication time (Hong Kong time)
    url: str
    currency: str
    is_quarterly_table: bool  # True: the highlights table has single-quarter data; False: year-to-date cumulative data
    values: dict[str, list[float]]  # series -> values of the row (equity: end of this period, end of last year)
    market: str = "HK"  # listing market, decides the closing time
    provisional: bool = False  # preliminary data, replaced when the official report is out
    shares_basis: str = ""  # period the disclosed weighted average shares refer to: empty means this period, "full year" means the full-year figure is disclosed (Q4), "year to date" the six or nine months (Sea)
    shares_label: str = "basic EPS weighted average shares"  # what kind of share count the filing discloses (A-share companies disclose the period-end total shares)
    note: str = ""  # explanation of the publication time etc.
    series_notes: dict[str, str] = field(default_factory=dict)  # series -> note on its figures in this filing (Parsed.notes), carried to each period value
    per_share_factors: tuple[float, float] = (1.0, 1.0)  # set by adjust_for_splits: what per-share figures were divided by (this and the previous period; prior year)
    supplement: bool = False  # a supplementary document of the results (XP's press release): it only adds its Non-GAAP EPS for its own quarter, see build_history

    @property
    def available(self) -> pd.Timestamp:
        """The date of the first closing price that can use this filing."""
        return get_market(self.market).available_date(self.published)


@dataclass
class Observation:
    value: float
    url: str
    derived: bool  # whether obtained by subtracting cumulative figures


@dataclass
class QuarterValue:
    value: float
    available: pd.Timestamp
    published: pd.Timestamp  # publication time of the earliest filing that published this value
    observations: list[Observation] = field(default_factory=list)
    factor: float = 1.0  # what the filing's figure was divided by for splits (per-share data), to show the original figure
    note: str = ""  # the note of the filing that first published this value (Announcement.series_notes), shown with the TTM formulas

    @property
    def max_deviation(self) -> float:
        return max(abs(o.value - self.value) for o in self.observations)


def parse_period(title: str) -> pd.Period:
    m = PERIOD_RE.search(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    return pd.Period(year=int(m.group(3)), quarter=MONTH_TO_QUARTER[m.group(2).upper()], freq="Q")


def period_label(p: pd.Period, periods_per_year: int) -> str:
    """Display name of a period: 2025Q4 for quarterly reporters, 2025H2 for half-yearly reporters."""
    if periods_per_year == 4:
        return str(p)
    if periods_per_year == 2:
        return f"{p.year}H{p.quarter // 2}"
    return f"{p.year}FY"


def to_number(token: str) -> float:
    value = float(token.strip("()").replace(",", ""))
    return -value if token.startswith("(") else value


def amounts_after(text: str, start: int, count: int) -> list[float]:
    """Take `count` amounts in turn from `start` (skipping percentages, footnote markers, etc.)."""
    out = []
    for token in text[start:].split():
        if AMOUNT_RE.fullmatch(token) and not FOOTNOTE_RE.fullmatch(token):
            out.append(to_number(token))
            if len(out) == count:
                break
    return out


class Pages:
    """Extract the text of the PDF pages on demand (whitespace collapsed to single spaces); pages not needed are not extracted."""

    def __init__(self, pdf: bytes):
        self._pages = PdfReader(io.BytesIO(pdf)).pages
        self._text: dict[int, str] = {}

    def __len__(self) -> int:
        return len(self._pages)

    def __getitem__(self, i: int) -> str:
        if i not in self._text:
            self._text[i] = " ".join((self._pages[i].extract_text() or "").split())
        return self._text[i]


def parse_summary_table(text: str) -> tuple[str, dict[str, list[float]]]:
    """From the highlights table on page 1 extract EPS, Non-IFRS EPS, revenue and net profit attributable to owners; returns (currency, values of each row)."""
    text = " ".join(text.split())
    values: dict[str, list[float]] = {}
    currency = None
    for m in EPS_RE.finditer(text):
        key = "non_ifrs_eps" if m.group("non_ifrs") else "eps"
        if key in values:
            continue
        values[key] = [to_number(t) for t in m.group("body").split() if NUMBER_RE.fullmatch(t)]
        currency = currency or CURRENCY_CODES.get(m.group("ccy"), m.group("ccy"))
    if "eps" not in values:
        raise ValueError("The EPS row was not found")

    count = len(values["eps"])
    for key, pattern in (("revenue", REVENUE_RE), ("profit", PROFIT_RE)):
        m = pattern.search(text)
        if m:
            values[key] = amounts_after(text, m.end(), count)
    return currency, values


def parse_equity(text: str) -> list[float] | None:
    """From the balance sheet extract equity attributable to owners (end of this period, end of last year)."""
    m = EQUITY_RE.search(" ".join(text.split()))
    if not m:
        return None
    tokens = [t for t in m.group("body").split() if AMOUNT_RE.fullmatch(t)]
    return [to_number(t) for t in tokens[-2:]] if len(tokens) >= 2 else None


def parse_highlights(pages: Pages) -> tuple[str, dict[str, list[float]]]:
    """Highlights table on page 1 + balance sheet (Tencent)."""
    currency, values = parse_summary_table(pages[0])
    for i in range(1, len(pages)):
        equity = parse_equity(pages[i])
        if equity:
            values["equity"] = equity
            break
    for i in range(len(pages)):
        if m := WA_SHARES_RE.search(" ".join(pages[i].split()) + " "):
            values["wa_shares"] = [to_number(t) for t in m.group(1).split()]  # keep all values first; parse_announcement picks the two of the matching period
            break
    return currency, values


def first_profit_match(text: str) -> re.Match | None:
    """Net profit attributable to owners row: only the one immediately preceded by the "Profit for the period/year" label counts."""
    for m in PROFIT_STMT_RE.finditer(text):
        if PROFIT_LABEL_RE.search(text[max(0, m.start() - 45) : m.start()]):
            return m
    return None


def parse_statements(pages: Pages) -> tuple[str, dict[str, list[float]]]:
    """Consolidated income statement and balance sheet (Sunny Optical). Amounts are converted to millions, EPS to yuan.

    In the income statement, next to the net profit attributable to owners there is a "total comprehensive income" row written the same way, and in the balance sheet
    "equity attributable to owners" ends with the same words. So net profit must be inside the income statement area (after the revenue row, before the EPS row)
    and be preceded by the label "Profit for the period/year". If it is not found an error is raised; another row is never taken by mistake.
    """
    start = next((i for i in range(len(pages)) if REVENUE_STMT_RE.search(" ".join(pages[i].split()))), None)
    if start is None:
        raise ValueError("The consolidated income statement was not found")

    values: dict[str, list[float]] = {}
    currency = None
    scale = None  # factor converting amounts to millions

    def amounts(m: re.Match) -> list[float]:
        if scale is None:
            raise ValueError("Cannot find the unit of the amounts")
        return [to_number(m.group("a")) * scale, to_number(m.group("b")) * scale]

    for i in range(start, len(pages)):
        text = " ".join(pages[i].split())
        unit = UNIT_RE.search(text)
        if unit:  # unit in the table header; when the balance sheet spans pages, later pages reuse the earlier unit
            scale = 0.001 if unit.group("unit") == "000" else 1.0

        eps_match = EPS_STMT_RE.search(text) if "eps" not in values else None
        if "revenue" not in values and (m := REVENUE_STMT_RE.search(text)):
            values["revenue"] = amounts(m)
        if "profit" not in values and "eps" not in values:  # only look before the EPS row
            if m := first_profit_match(text[: eps_match.start()] if eps_match else text):
                values["profit"] = amounts(m)
        if eps_match:
            per_unit = 0.01 if eps_match.group("cents") else 1.0
            values["eps"] = [to_number(eps_match.group("a")) * per_unit, to_number(eps_match.group("b")) * per_unit]
            currency = CURRENCY_CODES[eps_match.group("ccy").upper()]
        if "equity" not in values and "eps" in values and (m := EQUITY_STMT_RE.search(text)):
            values["equity"] = amounts(m)  # the balance sheet comes after the income statement
        if {"revenue", "profit", "equity", "eps"} <= values.keys():
            break
    missing = {"revenue", "profit", "equity", "eps"} - values.keys()
    if missing:
        raise ValueError(f"Not found: {sorted(missing)}")
    for i in range(len(pages)):
        if m := WA_SHARES_STMT_RE.search(" ".join(pages[i].split())):
            values["wa_shares"] = [to_number(m.group("a")) / 1000, to_number(m.group("b")) / 1000]  # thousands of shares -> millions
            break
    return currency, values


FORMATS = {"highlights": parse_highlights, "statements": parse_statements}  # HKEX PDF announcements
FILING_FORMATS = {*FORMATS, "press_release", "us_press_release", "us_gaap_statements", "ifrs_statements", "sap_ifrs", "kifrs_statements", "cn_report", "tesla_update"}  # plus US HTML press releases (6-K of JD; 8-K of US companies), IFRS statements (6-K of XP and SAP), Samsung English financial statements, A-share reports and Tesla's Update decks


def parse_announcement(filing: Filing, fmt: str = "highlights", market: Market = HK, non_gaap: str | None = None) -> Announcement:
    """Parse one earnings filing; values stay as in the filing (amounts converted to millions, EPS to yuan; not adjusted for splits).

    non_gaap is the EPS basis of the stock's Non-GAAP PE, None without one; only the us_press_release and tesla_update layouts use it (see parse_us_release, parse_tesla_update)."""
    if filing.supplement:  # a supplementary document whatever the layout of the main filings: only its Non-GAAP EPS is read
        parsed = parse_adjusted_eps_release(filing.content)
        return Announcement(parsed.period, filing.published, filing.url, parsed.currency, True, parsed.values, market.key, note=filing.note, supplement=True)
    if fmt == "cn_report":  # A-shares: periodic report or express report (preliminary)
        parsed = parse_cn_express(filing.title, filing.content) if "express" in filing.title else parse_cn_report(filing.title, filing.content)
        return Announcement(
            parsed.period, filing.published, filing.url, parsed.currency, parsed.quarterly, parsed.values, market.key,
            filing.provisional, "", PERIOD_END_SHARES, filing.note,
        )
    if fmt in ("press_release", "us_press_release", "us_gaap_statements", "ifrs_statements", "sap_ifrs", "kifrs_statements", "tesla_update"):  # the parse result carries its own reporting period and whether it is a single quarter
        if fmt == "tesla_update":
            parsed = parse_tesla_filing(filing, non_gaap)
        elif fmt == "press_release":
            parsed = parse_press_release(filing.content)
        elif fmt == "us_press_release":
            parsed = parse_us_release(filing.content, non_gaap)
        elif fmt == "us_gaap_statements":
            parsed = parse_us_release(filing.content)
        elif fmt == "ifrs_statements":
            parsed = parse_ifrs_statements(filing.content)
        elif fmt == "sap_ifrs":
            parsed = parse_sap_statement(filing.content)
        elif filing.provisional:
            parsed = parse_kifrs_deck(filing.title, filing.content, filing.attachments["previous_bs"])
        else:
            parsed = parse_kifrs(filing.title, filing.content, filing.attachments["bs"], filing.attachments.get("note"))
        basis = "" if parsed.quarterly else "full year" if parsed.period.quarter == 4 else YEAR_TO_DATE  # Sea's second- and third-quarter releases: six and nine months
        return Announcement(
            parsed.period, filing.published, filing.url, parsed.currency, parsed.quarterly, parsed.values, market.key,
            filing.provisional, basis, series_notes=dict(parsed.notes or {}),
        )
    period = parse_period(filing.title)
    currency, values = FORMATS[fmt](Pages(filing.content))
    shares = values.pop("wa_shares", None)
    counts = {len(values[k]) for k in FLOWS if k in values}
    if counts == {3}:
        quarterly = True
    elif counts == {2}:
        quarterly = period.quarter == 1  # the "cumulative" figure of a Q1 filing is the single quarter
    else:
        raise ValueError(f"{filing.url}: unexpected number of values per row: {values}")
    basis = ""
    if shares is not None:
        # Tencent's note has two numbers each for three months and cumulative (this period, same period last year): take the three-month ones, matching this quarter;
        # only two numbers means Q1 (three months) or the full year (the Q4 filing does not list Q4 shares separately)
        basis = "full year" if period.quarter == 4 and (len(shares) == 2 or fmt == "statements") else ""
        values["wa_shares"] = shares[:2]
    return Announcement(period, filing.published, filing.url, currency, quarterly, values, market.key, False, basis)


PERIOD_END_SHARES = "period-end total shares"
YEAR_TO_DATE = "year to date"  # shares basis of a year-to-date filing before the fourth quarter
PRIOR_BASIS_TOLERANCE = 0.05  # relative: a prior-year EPS vs the earlier filing's figure for that period (both rounded the same way)
CURRENT_BASIS_TOLERANCE = 0.15  # relative: net profit / EPS vs the filing's share count (period-end shares differ from the weighted average)


def split_factor(after: pd.Timestamp, splits: pd.Series) -> float:
    """The product of the splits that took effect after this time: what a per-share figure on the share basis of that time
    must be divided by (and a share count multiplied by) to be on the current basis of the split-adjusted prices."""
    if splits.empty:
        return 1.0
    later = splits[splits.index > after.normalize()]
    return float(later.prod()) if len(later) else 1.0


def _snap(needed: float, candidates: set[float], usual: float, tolerance: float) -> float:
    """The candidate factor closest to what the evidence needs; the usual one unless another fits clearly better."""
    best = min(candidates, key=lambda c: abs(c / needed - 1))
    if best == usual or abs(best / needed - 1) > tolerance or abs(best / needed - 1) >= abs(usual / needed - 1):
        return usual
    return best


def per_share_basis(a: Announcement, earlier: list[Announcement], splits: pd.Series, shares_factor: float) -> tuple[float, float, str]:
    """The split factors for the per-share figures of a filing: (this period and the previous period, the prior-year column), and a note when one is not the usual.

    Usually per-share figures are on the share basis of the filing's publication (accounting standards restate EPS for splits
    before the report is issued). When a split took effect during the period the filing covers or before it was published,
    the filing's own figures decide, column by column, using only what was published by then:
      this period     profit / EPS gives the shares the EPS was computed on; they must match the filing's share count
                      (Zhongji Innolight's 2024 half-year report: 2,358 / 3.03 = 778 million shares, before the June 2024
                      bonus issue, while its share count is 1,121 million, after it)
      prior year      the figure must match what the earlier filing for that period reported (Innolight's 2019 and 2024
                      half-year reports repeat the earlier 0.67 and 0.77 unchanged, though the 2019 current figure is restated)
    """
    usual = split_factor(a.published, splits)
    if splits.empty:
        return usual, usual, ""
    span_start = a.period.start_time - pd.DateOffset(months=9)  # a year-to-date figure covers up to four quarters
    during = splits[(splits.index > span_start) & (splits.index <= a.published.normalize())]
    eps = a.values.get("eps") or []
    if during.empty or not eps:
        return usual, usual, ""
    candidates = {split_factor(t, splits) for t in [span_start, *during.index, a.published]}
    notes = []

    current = usual
    profit, shares = (a.values.get("profit") or [None])[0], (a.values.get("wa_shares") or [None])[0]
    if eps[0] and profit and shares:
        implied = profit / eps[0]
        current = _snap(shares * shares_factor / implied, candidates, usual, CURRENT_BASIS_TOLERANCE)
        if current != usual:
            notes.append(
                f"the EPS {num(eps[0])} is on the shares before the split (net profit / EPS = {implied:,.1f} million shares, the "
                f"filing's share count is {shares:,.1f} million), so it is divided by a further {num(current / usual)}"
            )

    prior = current
    previous = [
        e for e in earlier
        if e.period == a.period - 4 and e.is_quarterly_table == a.is_quarterly_table and e.published < a.published and (e.values.get("eps") or [0])[0]
    ]
    if len(eps) > 1 and eps[1] and previous:
        before = min(previous, key=lambda e: e.published)
        # On the current basis the prior-year figure must equal the earlier filing's: prior / factor = earlier figure / its factor
        needed = split_factor(before.published, splits) * eps[1] / before.values["eps"][0]
        prior = _snap(needed, candidates, current, PRIOR_BASIS_TOLERANCE)
        if prior != current:
            notes.append(
                f"its prior-year EPS {num(eps[1])} repeats the earlier filing's {num(before.values['eps'][0])} without restating it, "
                f"so it is adjusted like that figure (divided by {num(prior)} in all)"
            )
    note = ("Split basis: " + "; ".join(notes)) if notes else ""
    return current, prior, note


def adjust_for_splits(announcements: list[Announcement], splits: pd.Series) -> list[Announcement]:
    """Convert per-share data and share counts to the current share basis (consistent with split-adjusted prices); amounts are not affected by splits.

    Share counts: a weighted average disclosed with EPS is restated like EPS, so its basis is that of the publication; the
    period-end total shares of A-share companies are on the basis of the period end. Per-share figures: see per_share_basis.
    """
    out = []
    for a in announcements:
        shares_factor = split_factor(a.period.end_time if a.shares_label == PERIOD_END_SHARES else a.published, splits)
        current, prior, note = per_share_basis(a, announcements, splits, shares_factor)
        factors = [current, prior, current]  # this period, same period last year, previous period
        values = {
            k: [v / f for v, f in zip(vs, factors)] if k in PER_SHARE else [v * shares_factor for v in vs] if k in SHARE_COUNTS else vs
            for k, vs in a.values.items()
        }
        out.append(replace(a, values=values, per_share_factors=(current, prior), note="; ".join(x for x in (a.note, note) if x)))
    return out


ANNOUNCEMENT_COLUMNS = ["published", "period", "table", "currency", *SERIES, "url", "provisional", "shares_basis", "shares_label", "note", "parser_version"]


SERIES_NOTE_PREFIX = {s: f"{label} of this filing: " for s, label in SERIES_LABELS.items()}  # how Announcement.series_notes are kept in the note column


def _series_note(part: str) -> tuple[str, str] | None:
    """A part of the saved note column that is a series note ("Non-IFRS EPS of this filing: ..."): (series, note); None for any other text."""
    return next(((s, part[len(prefix) :]) for s, prefix in SERIES_NOTE_PREFIX.items() if part.startswith(prefix)), None)


def announcements_to_frame(announcements: list[Announcement], parser_version: int) -> pd.DataFrame:
    """Storage format of parse results: one row per filing, values of a row joined with semicolons (numbers as in the filing).

    A supplementary document is stored with the table "supplement" and the version of its own parser (SUPPLEMENT_PARSER_VERSION)."""
    rows = [
        {
            "published": f"{a.published:%Y-%m-%d %H:%M}",
            "period": str(a.period),
            "table": SUPPLEMENT_TABLE if a.supplement else "quarterly" if a.is_quarterly_table else "cumulative",
            "currency": a.currency,
            **{s: ";".join(f"{v:.10g}" for v in a.values[s]) if s in a.values else "" for s in SERIES},
            "url": a.url,
            "provisional": int(a.provisional),
            "shares_basis": a.shares_basis,
            "shares_label": a.shares_label,
            "note": "; ".join(x for x in (a.note, *(f"{SERIES_NOTE_PREFIX[s]}{t}" for s, t in sorted(a.series_notes.items()))) if x),
            "parser_version": SUPPLEMENT_PARSER_VERSION if a.supplement else parser_version,
        }
        for a in sorted(announcements, key=lambda a: a.published)
    ]
    return pd.DataFrame(rows, columns=ANNOUNCEMENT_COLUMNS)


def announcements_from_frame(df: pd.DataFrame, market: Market = HK) -> list[Announcement]:
    out = []
    for r in df.fillna("").to_dict("records"):
        out.append(
            Announcement(
                period=pd.Period(r["period"], freq="Q"),
                published=pd.Timestamp(r["published"]),
                url=r["url"],
                currency=r["currency"],
                is_quarterly_table=r["table"] in ("quarterly", SUPPLEMENT_TABLE),
                values={s: [float(v) for v in str(r[s]).split(";")] for s in SERIES if str(r.get(s, ""))},
                market=market.key,
                provisional=str(r.get("provisional", "")) == "1",
                shares_basis=str(r.get("shares_basis", "")),
                shares_label=str(r.get("shares_label", "")) or "basic EPS weighted average shares",
                note="; ".join(x for x in str(r.get("note", "")).split("; ") if x and not _series_note(x)),
                series_notes=dict(n for x in str(r.get("note", "")).split("; ") if (n := _series_note(x))),
                supplement=r["table"] == SUPPLEMENT_TABLE,
            )
        )
    return out


def implied_shares(quarters: dict[str, dict[pd.Period, QuarterValue]], p: pd.Period) -> float | None:
    """Weighted average shares of the period (millions) = net profit attributable to owners of the period / basic EPS of the period."""
    profit, eps = quarters["profit"].get(p), quarters["eps"].get(p)
    if profit is None or eps is None or eps.value == 0:
        return None
    return profit.value / eps.value


@dataclass
class EarningsHistory:
    currency: str
    quarters: dict[str, dict[pd.Period, QuarterValue]]  # series -> period -> single-period value (equity: period-end value)
    ttm: pd.DataFrame  # data available after each filing, columns see TTM_COLUMNS
    announcements: list[Announcement]
    periods_per_year: int = 4  # earnings periods published per year: 4 (quarterly), 2 (half-yearly), 1 (annual)
    ttm_detail: list[dict] = field(default_factory=list)  # row by row matching ttm: how each item was calculated (see audit.py)
    shares_basis: dict[pd.Period, str] = field(default_factory=dict)  # period the disclosed weighted average shares of each period refer to (empty = this period, "full year", "year to date")
    non_gaap_changes: tuple[pd.Timestamp, ...] = ()  # when the company changed the definition of its Non-GAAP EPS (see build_history)

    @property
    def step(self) -> int:
        """How many quarters lie between adjacent periods."""
        return 4 // self.periods_per_year

    def discrepancies(self) -> list[str]:
        out = []
        for series, quarters in self.quarters.items():
            fmt = "{:.3f}" if series in PER_SHARE else "{:,.0f}"
            for p, q in sorted(quarters.items()):
                if q.max_deviation > tolerance(series, q.value):
                    vals = ", ".join(fmt.format(o.value) + (" (derived from cumulative figures)" if o.derived else "") for o in q.observations)
                    out.append(
                        f"{SERIES_LABELS[series]} {period_label(p, self.periods_per_year)} differs between filings (probably a restatement) [{vals}]; "
                        f"using the first published value {fmt.format(q.value)}"
                    )
        for a in self.announcements:
            if a.provisional:
                out.append(f"The {period_label(a.period, self.periods_per_year)} data {PROVISIONAL_NOTES.get(a.market, PROVISIONAL_NOTES['*'])}")
        for change in self.non_gaap_changes:
            after = self.ttm[pd.to_datetime(self.ttm["published"]) >= change]
            empty = after["non_ifrs_eps_ttm"].isna()
            resumed = after[~empty]
            out.append(
                f"The company changed the definition of its Non-GAAP EPS with the filing published {change:%Y-%m-%d}: the Non-GAAP EPS TTM is only taken "
                "from four quarters of one definition (the figures published since then; earlier quarters keep their first published figures under the "
                "old one), so it is empty "
                + (f"until the filing published {resumed['published'].iloc[0]}" if len(resumed) and empty.iloc[0] else "for the filings since then" if empty.all() else "for no filing")
            )
        shares = {p: s for p in sorted(self.quarters["eps"]) if (s := implied_shares(self.quarters, p))}
        periods = list(shares)
        label = lambda p: period_label(p, self.periods_per_year)  # noqa: E731
        for prev, cur in zip(periods, periods[1:]):
            if cur - prev == self.step and abs(shares[cur] / shares[prev] - 1) > SHARES_CHANGE_LIMIT:
                out.append(
                    f"Implied shares {label(prev)} {shares[prev]:,.0f} -> {label(cur)} {shares[cur]:,.0f} million changed by more than 5%; please check"
                )
        return out

    def periods_frame(self, trading_days: pd.DatetimeIndex | None = None) -> pd.DataFrame:
        """Per-period detail (quarterly or half-yearly). When trading days are given, also lists the trading day each value actually starts to be used."""
        rows = []
        for p in sorted(set().union(*self.quarters.values())):
            row = {"period": period_label(p, self.periods_per_year), "currency": self.currency}
            for series in SERIES:
                q = self.quarters[series].get(p)
                row[series] = round(q.value, 4) if q else None
                row[f"{series}_published"] = f"{q.published:%Y-%m-%d %H:%M}" if q else None
                row[f"{series}_effective"] = effective_day(q.available, trading_days) if q else None
                row[f"{series}_sources"] = len(q.observations) if q else 0
                row[f"{series}_max_deviation"] = round(q.max_deviation, 4) if q else None
            shares = implied_shares(self.quarters, p)
            row["shares"] = round(shares, 1) if shares else None
            row["first_source"] = next(
                (q.observations[0].url for s in SERIES if (q := self.quarters[s].get(p))), None
            )
            rows.append(row)
        return pd.DataFrame(rows)

    def ttm_frame(self, trading_days: pd.DatetimeIndex | None = None) -> pd.DataFrame:
        """The data available after each filing, with the calculation: values of the periods the TTM covers, formulas and intermediate quantities."""
        df = self.ttm.copy()
        df.insert(3, "effective_date", [effective_day(d, trading_days) for d in df["available_date"]])
        slots = 4
        extra = {"provisional": [int(r["provisional"]) for r in self.ttm_detail]}
        for k in range(slots):
            extra[f"period_{k + 1}"] = [r["periods"][k] if k < len(r["periods"]) else None for r in self.ttm_detail]
        for series in ("eps", "non_ifrs_eps", "revenue"):
            for k in range(slots):
                extra[f"{series}_{k + 1}"] = [
                    r[series][k].value if r[series] and k < len(r[series]) else None for r in self.ttm_detail
                ]
        extra["profit_latest"] = [r["profit"] for r in self.ttm_detail]
        extra["eps_latest"] = [r["eps_latest"] for r in self.ttm_detail]
        extra["eps_ttm_formula"] = [sum_formula(r["eps"], r["eps_ttm"]) for r in self.ttm_detail]
        extra["non_ifrs_eps_ttm_formula"] = [sum_formula(r["non_ifrs_eps"], r["non_ifrs_eps_ttm"]) for r in self.ttm_detail]
        extra["revenue_ttm_formula"] = [sum_formula(r["revenue"], r["revenue_ttm"]) for r in self.ttm_detail]
        extra["shares_formula"] = [shares_formula(r) for r in self.ttm_detail]
        extra["shares_source"] = [_shares_source_label(r) for r in self.ttm_detail]
        extra["shares_implied"] = [r["shares_implied"] for r in self.ttm_detail]
        extra["revenue_per_share_formula"] = [
            _division(r["revenue_ttm"], r["shares"], r["revenue_per_share_ttm"]) for r in self.ttm_detail
        ]
        extra["book_value_per_share_formula"] = [_division(r["equity"], r["shares"], r["book_value_per_share"]) for r in self.ttm_detail]
        extra["published_note"] = [r["note"] for r in self.ttm_detail]
        extra["split_note"] = [
            "; ".join(split_notes(r["eps"]) + split_notes(r["non_ifrs_eps"])) for r in self.ttm_detail
        ]
        df = pd.concat([df.reset_index(drop=True), pd.DataFrame(extra)], axis=1)
        return df[[c for c in TTM_FRAME_ORDER if c in df]]


def _division(numerator: float | None, denominator: float | None, result: float | None) -> str:
    if numerator is None or not denominator or result is None:
        return ""
    return f"{num(numerator)} / {num(denominator)} = {num(result)}"


def effective_day(available: pd.Timestamp, trading_days: pd.DatetimeIndex | None):
    """The trading day (date) it actually starts to be used; empty when the price data does not have that day yet (market not yet open) or no trading days were given."""
    if trading_days is None:
        return None
    day = effective_trading_day(available, trading_days)
    return day.date() if day is not None else None


def infer_periods_per_year(periods: list[pd.Period]) -> int:
    """Periods published per year: a filing for Q1 or Q3 means quarterly reporting; only Q2 (interim) means half-yearly."""
    quarters = {p.quarter for p in periods}
    if quarters & {1, 3}:
        return 4
    return 2 if 2 in quarters else 1


def build_history(parsed: list[Announcement], splits: pd.Series | None = None, non_gaap_changes: tuple[str | pd.Timestamp, ...] = ()) -> EarningsHistory:
    """Arrange the parsed filings into per-period values and the data available after each filing.

    non_gaap_changes are the days the company changed the definition of its Non-GAAP EPS (Cognizant: 2019-02-06, from "Non-GAAP diluted EPS" to
    "Adjusted Diluted EPS"). A Non-GAAP figure belongs to the definition in force when its filing was published, and each definition keeps the first
    published figure of every quarter on its own: a later filing's figure for an earlier quarter under the new definition is not a restatement of the
    old one. A TTM row takes its Non-GAAP EPS only from figures of the definition of its own filing; while the new definition does not yet have four
    quarters, the Non-GAAP EPS TTM is empty. The per-period values (quarters) keep the first published figure of each quarter, whatever its definition.

    Supplementary documents (XP's press release next to its IFRS statements) only add their Non-GAAP EPS, for their own quarter and with their own
    publication time. One published on the day its quarter's TTM row becomes available (before or after the statements) completes that row; one that
    becomes available later adds a row of its own, a copy of the latest row with the Non-GAAP EPS TTM brought up to date. A supplement without a
    value, or for a quarter that has no filing, changes nothing. Without supplements the result is exactly what it was before they existed.
    """
    supplements = sorted((a for a in parsed if a.supplement), key=lambda x: (x.available, x.published))
    parsed = [a for a in parsed if not a.supplement]
    announcements: dict[pd.Period, Announcement] = {}
    for a in sorted(parsed, key=lambda x: x.available):
        announcements.setdefault(a.period, a)  # for duplicate filings of the same reporting period only the earliest is taken

    currencies = {a.currency for a in announcements.values()}
    if len(currencies) != 1:
        raise ValueError(f"The filings disagree on the EPS currency: {currencies}")
    per_year = infer_periods_per_year(list(announcements))
    step = 4 // per_year

    quarters: dict[str, dict[pd.Period, QuarterValue]] = {s: {} for s in SERIES}
    ytd: dict[str, dict[pd.Period, float]] = {s: {} for s in FLOWS}
    ttm_rows, details = [], []
    shares_basis: dict[pd.Period, str] = {}
    changes = tuple(sorted(pd.Timestamp(c) for c in non_gaap_changes))
    definitions: dict[int, dict[pd.Period, QuarterValue]] = {}  # with non_gaap_changes: definition (0, 1, ...) -> the Non-GAAP EPS of each quarter
    definition_of = lambda a: sum(a.published >= c for c in changes)  # noqa: E731
    first_definition: dict[pd.Period, int] = {}

    def record(series: str, period: pd.Period, value: float, a: Announcement, derived: bool) -> None:
        factor = (a.per_share_factors[1] if period == a.period - 4 else a.per_share_factors[0]) if series in PER_SHARE else 1.0
        if series == "non_ifrs_eps" and changes:
            if derived:
                raise ValueError(f"{a.url}: a Non-GAAP EPS from cumulative figures cannot be told apart by definition (non_gaap_changes)")
            d = definition_of(a)
            own = definitions.setdefault(d, {}).setdefault(period, QuarterValue(value, a.available, a.published, factor=factor, note=a.series_notes.get(series, "")))
            own.observations.append(Observation(value, a.url, derived))
            if first_definition.setdefault(period, d) != d:
                return  # another definition's figure for a quarter first published under an earlier one: not compared with it
        q = quarters[series].setdefault(period, QuarterValue(value, a.available, a.published, factor=factor, note=a.series_notes.get(series, "")))
        q.observations.append(Observation(value, a.url, derived))

    def row_of(a: Announcement) -> tuple[dict, dict]:
        own = definitions.get(definition_of(a), {}) if changes else None
        return _ttm_row(a, quarters, per_year, splits if splits is not None else pd.Series(dtype=float), shares_basis, own)

    pending = list(supplements)
    rows_of: list[Announcement] = []  # the filing each TTM row is for

    def add_supplements(until: pd.Timestamp | None) -> None:
        """Record the supplements available before `until` (all when None); refresh the rows they change."""
        while pending and (until is None or pending[0].available < until):
            s = pending.pop(0)
            if s.currency not in currencies:
                raise ValueError(f"{s.url}: the supplementary document is in {s.currency}, the filings in {currencies}")
            for series, vals in s.values.items():
                if series != "non_ifrs_eps":
                    raise ValueError(f"{s.url}: a supplementary document only adds the Non-GAAP EPS, got {series}")
                record(series, s.period, vals[0], s, derived=False)
            if not s.values or not rows_of or not 0 <= (rows_of[-1].period - s.period).n < 4:
                continue  # nothing to add, or not part of the latest TTM: its own quarter's row will use it
            latest = rows_of[-1]
            row, detail = row_of(latest)
            if s.available == ttm_rows[-1]["available_date"]:  # available on the same day as the latest row: that row is completed
                ttm_rows[-1], details[-1] = row, detail
            else:  # available later: a row of its own, from the day the press release can be used
                row = {**row, "published": f"{s.published:%Y-%m-%d %H:%M}", "available_date": s.available}
                detail = {**detail, "published": row["published"], "note": "; ".join(x for x in (detail["note"], f"Non-GAAP EPS from the press release published {row['published']}") if x)}
                ttm_rows.append(row)
                details.append(detail)
                rows_of.append(latest)

    for a in sorted(announcements.values(), key=lambda x: x.available):
        add_supplements(a.available)
        for series, vals in a.values.items():
            touched: list[tuple[pd.Period, float]] = []  # the year-to-date cumulative figures in this filing
            if series in BALANCES:
                prior_year_end = pd.Period(year=a.period.year - 1, quarter=4, freq="Q")
                for period, value in zip([a.period, prior_year_end], vals):
                    record(series, period, value, a, derived=False)
            elif series in SHARE_COUNTS:  # shares disclosed in the filing: the value of that period itself, cannot be obtained by subtracting cumulative figures
                for period, value in zip([a.period, a.period - 4, a.period - 1], vals):
                    if a.shares_basis == YEAR_TO_DATE and shares_basis.get(period, YEAR_TO_DATE) != YEAR_TO_DATE:
                        continue  # a year-to-date average is not comparable with the three-month figure an earlier filing gave for the period (Sea 2017Q3)
                    record(series, period, value, a, derived=False)
                    shares_basis.setdefault(period, a.shares_basis)
            elif a.is_quarterly_table:
                for period, value in zip([a.period, a.period - 4, a.period - 1], vals):
                    record(series, period, value, a, derived=False)
            else:
                for period, value in zip([a.period, a.period - 4], vals):
                    ytd[series].setdefault(period, value)
                    touched.append((period, value))
            if series in FLOWS:
                _derive_from_ytd(series, quarters[series], ytd[series], a, record, step)
                _cross_check_ytd(quarters[series], touched, a, step)

        row, detail = row_of(a)
        ttm_rows.append(row)
        details.append(detail)
        rows_of.append(a)
        add_supplements(a.available + pd.Timedelta(days=1))  # those of the same day complete this row
    add_supplements(None)

    ttm = pd.DataFrame(ttm_rows, columns=TTM_COLUMNS)
    return EarningsHistory(currencies.pop(), quarters, ttm, [*announcements.values(), *supplements], per_year, details, shares_basis, changes)


def _ttm_row(
    a: Announcement, quarters: dict[str, dict[pd.Period, QuarterValue]], per_year: int, splits: pd.Series, shares_basis: dict[pd.Period, str],
    non_gaap: dict[pd.Period, QuarterValue] | None = None,
) -> tuple[dict, dict]:
    """The data available after this filing is published: each TTM (sum of the periods in the last year), shares, revenue per share, book value per share.

    Returns (row of values, calculation). The calculation records which values of which periods add up to each TTM, for checking.
    non_gaap, when given, are the Non-GAAP EPS of the definition of this filing (see build_history), used instead of quarters["non_ifrs_eps"].
    """
    last_year = [a.period - k * (4 // per_year) for k in range(per_year)]
    periods = list(reversed(last_year))  # oldest to newest

    def components(series: str) -> list[Component] | None:
        qs = non_gaap if series == "non_ifrs_eps" and non_gaap is not None else quarters[series]
        if not all(p in qs for p in periods):
            return None
        out = []
        for p in periods:
            q = qs[p]
            derived = q.observations[0].derived
            factor = q.factor if series in PER_SHARE and not derived else 1.0
            out.append(Component(period_label(p, per_year), q.value, derived, q.value * factor if factor != 1.0 else None, q.note))
        return out

    def total(comps: list[Component] | None) -> float | None:
        return sum(c.value for c in comps) if comps else None

    def rounded(value: float | None, digits: int = 4) -> float | None:
        return round(value, digits) if value is not None else None

    eps, non_ifrs, revenue = components("eps"), components("non_ifrs_eps"), components("revenue")
    implied = implied_shares(quarters, a.period)
    reported = quarters["wa_shares"].get(a.period)
    basis = shares_basis.get(a.period, "") if reported else ""
    shares, source, parts = (reported.value, "reported", None) if reported else (implied, "implied", None)
    if reported and basis:
        # the filing only discloses the full year: the full-year figure is the day-weighted average of the quarters, so this period (Q4 or the second half) = full year x days in year - earlier periods x their days, divided by the days of this period
        parts = _fiscal_year_parts(a.period, reported.value, quarters["wa_shares"], shares_basis, 4 // per_year)
        if parts:
            shares, source = parts["shares"], "derived"
            for x in parts["parts"]:
                x["period"] = f"{x['period'].year}Q1-Q{x['period'].quarter}" if x.get("year_to_date") else period_label(x["period"], per_year)
        else:
            shares, source, basis = implied, "implied", ""  # the earlier periods have no disclosed shares either: fall back to implied
    revenue_ttm = total(revenue)
    equity = quarters["equity"].get(a.period)
    row = {
        "report_date": a.period.end_time.normalize(),
        "published": f"{a.published:%Y-%m-%d %H:%M}",
        "available_date": a.available,
        "eps_ttm": rounded(total(eps)),
        "non_ifrs_eps_ttm": rounded(total(non_ifrs)),
        "revenue_ttm": rounded(revenue_ttm, 1),
        "shares": rounded(shares, 3),
        "revenue_per_share_ttm": rounded(revenue_ttm / shares) if revenue_ttm is not None and shares else None,
        "equity": rounded(equity.value, 1) if equity else None,
        "book_value_per_share": rounded(equity.value / shares) if equity and shares else None,
    }
    profit, eps_now = quarters["profit"].get(a.period), quarters["eps"].get(a.period)
    detail = {
        "period_label": period_label(a.period, per_year),
        "published": f"{a.published:%Y-%m-%d %H:%M}",
        "provisional": a.provisional,
        "periods": [period_label(p, per_year) for p in periods],
        "eps": eps,
        "non_ifrs_eps": non_ifrs,
        "revenue": revenue,
        "eps_ttm": row["eps_ttm"],
        "non_ifrs_eps_ttm": row["non_ifrs_eps_ttm"],
        "revenue_ttm": row["revenue_ttm"],
        "profit": profit.value if profit else None,
        "eps_latest": eps_now.value if eps_now else None,
        "shares": row["shares"],
        "shares_label": a.shares_label,
        "note": a.note,
        "shares_source": source,
        "shares_basis": basis,
        "shares_parts": parts,
        "shares_implied": implied,
        "equity": row["equity"],
        "revenue_per_share_ttm": row["revenue_per_share_ttm"],
        "book_value_per_share": row["book_value_per_share"],
    }
    return row, detail


def _days(p: pd.Period, step: int) -> int:
    """Days of a period; step is the number of quarters in a period (a half-year period is two quarters)."""
    return (p.end_time.normalize() - (p - (step - 1)).start_time.normalize()).days + 1


def _fiscal_year_parts(
    p: pd.Period, full_year: float, shares: dict[pd.Period, QuarterValue], basis: dict[pd.Period, str], step: int
) -> dict | None:
    """Full-year (or year-to-date) weighted average shares -> weighted average shares of this period (the last period they cover).

    Needs the shares disclosed for the earlier periods of the year: each on its own, or the latest of them as a year-to-date figure that covers the rest
    (Sea's third quarter: nine months x 273 days - six months x 181 days)."""
    earlier = sorted(_earlier_periods(p, step))
    if not earlier:
        return None
    ytd = [e for e in earlier if e in shares and basis.get(e, "") == YEAR_TO_DATE]
    if ytd:  # the latest year-to-date figure covers the periods up to it
        earlier = [e for e in earlier if e > ytd[-1]]
        first = {"period": ytd[-1], "shares": shares[ytd[-1]].value, "days": _ytd_days(ytd[-1]), "year_to_date": True}
    if any(e not in shares or basis.get(e, "") for e in earlier):
        return None
    year_days = (pd.Timestamp(year=p.year, month=12, day=31) - pd.Timestamp(year=p.year, month=1, day=1)).days + 1 if p.quarter == 4 else _ytd_days(p)
    parts = ([first] if ytd else []) + [{"period": e, "shares": shares[e].value, "days": _days(e, step)} for e in earlier]
    days = _days(p, step)
    value = (full_year * year_days - sum(x["shares"] * x["days"] for x in parts)) / days
    return {"shares": value, "full_year": full_year, "year_days": year_days, "days": days, "parts": parts, **({"basis": YEAR_TO_DATE} if basis.get(p) == YEAR_TO_DATE else {})}


def _shares_source_label(detail: dict) -> str:
    if detail["shares_source"] == "derived" and (detail.get("shares_parts") or {}).get("basis") == YEAR_TO_DATE:
        return "derived from the year-to-date figure"
    return {"reported": "reported", "derived": "derived from the full-year figure", "implied": "implied (profit / EPS)"}[detail["shares_source"]]


def _ytd_days(p: pd.Period) -> int:
    """Days from the start of the year to the end of quarter p."""
    return (p.end_time.normalize() - pd.Timestamp(year=p.year, month=1, day=1)).days + 1


def _earlier_periods(p: pd.Period, step: int) -> list[pd.Period]:
    """The periods of the same year before p. step is the number of quarters between adjacent periods."""
    return [p - j * step for j in range(1, p.quarter // step)]


def _derive_from_ytd(series, quarters, ytd, a, record, step: int) -> None:
    """Derive a single-period value not yet known from year-to-date cumulative figures: this period = cumulative(this period) - cumulative(previous period), or - the sum of the earlier periods of the year.

    For quarterly reporters the periods within a year are quarters; for half-yearly reporters the cumulative first-half figure is the first half itself,
    and the second half = full year - first half.
    """
    changed = True
    while changed:
        changed = False
        for p, total in ytd.items():
            if p in quarters:
                continue
            earlier = _earlier_periods(p, step)
            if not earlier:
                value = total
            elif (p - step) in ytd:
                value = total - ytd[p - step]
            elif all(e in quarters for e in earlier):
                value = total - sum(quarters[e].value for e in earlier)
            else:
                continue
            record(series, p, value, a, derived=True)
            changed = True


def _cross_check_ytd(quarters, touched, a, step: int) -> None:
    """The cumulative figures in this filing (including the same period last year) can also give the values of periods; used as another source of the existing values, for cross-checking."""
    for p, total in touched:
        q = quarters.get(p)
        earlier = _earlier_periods(p, step)
        if q is None or any(o.url == a.url for o in q.observations) or not all(e in quarters for e in earlier):
            continue
        q.observations.append(Observation(total - sum(quarters[e].value for e in earlier), a.url, True))
