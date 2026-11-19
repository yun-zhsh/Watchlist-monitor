"""Extract earnings data from the Chinese periodic reports and express reports of A-share companies
(PDF originals; verified on all Hikvision, Glodon and Zhongji Innolight reports from 2014 on).

The report type decides which numbers are used:
  Q1 report        consolidated income statement, current period and prior period (single quarter)
  Half-year report consolidated income statement, current and prior period (cumulative, first half)
  Q3 report        the single-quarter Q3 figures are only in the "key accounting data" table (its first column)
  Annual report    consolidated income statement, current and prior period (full year, cumulative)
  Express report   preliminary annual figures (not audited by the accounting firm), published before the annual
                   report: revenue, net profit, EPS, equity, share capital
Revenue is the operating revenue: total operating revenue, or the operating revenue under it where the total also
includes interest and fee income (Glodon's finance business); express reports give only the total. Net profit is the
profit attributable to shareholders of the parent, EPS is basic EPS; amounts are in RMB and converted to millions. Equity is the equity attributable to owners of the parent
on the balance sheet; the share count is the balance sheet's share capital (period-end total shares, par value
RMB 1, so shares = the share capital amount).
In every periodic report the first column of the key-data table must match the same item in the consolidated
statements; otherwise parsing fails.

The Chinese patterns live in cn_patterns.py; this module is language-neutral.
"""

from __future__ import annotations

import io
import re

import pandas as pd
from pypdf import PdfReader

from valuation_monitor import cn_patterns as cn
from valuation_monitor.cid_fonts import add_missing_unicode_maps
from valuation_monitor.parsed import Parsed

NUM = r"(-?[\d,]+(?:\.\d+)?|\(\s*[\d,]+(?:\.\d+)?\s*\))"
MILLION = 1e6
TEN_THOUSAND = 1e4  # the unit of some express reports
HUNDRED_MILLION = 1e8  # the unit of banks' express reports
UNIT_SIZES = (1e3, TEN_THOUSAND, HUNDRED_MILLION)  # the groups of cn.UNIT_RE; a match of none of them means yuan
# Titles are English and built by the CNINFO provider: "2026 Q3 report", "2025 annual report", "2025 annual express report"
REPORT_TITLE_RE = re.compile(r"^(?P<year>\d{4}) (?P<kind>Q1|H1|Q3|annual) report$")
EXPRESS_TITLE_RE = re.compile(r"^(?P<year>\d{4}) (?P<kind>H1|annual) express report$")
QUARTER_OF = {"Q1": 1, "H1": 2, "Q3": 3, "annual": 4}


def clean_text(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(cn.CJK_GAP_RE, "", text)
    text = re.sub(r"(\d,\d{3}(?:,\d{3})*\.\d) (\d)(?![\d,])", r"\1\2", text)  # the last decimal digit gets split off, e.g. "158.1 1"
    # The last digit group of the integer part gets split, e.g. "9,335,806,1 14.00": merge only when the pieces add up to three digits
    # The thousands separators of an amount come out as a dot and spaces, e.g. "4.842 546 924 23" for 4,842,546,924.23
    text = re.sub(r"(?<![\d.,])(\d{1,3})\.(\d{3}) (\d{3}) (\d{3}) (\d{2})(?![\d.,%])", r"\1,\2,\3,\4.\5", text)
    return re.sub(
        r"(?<![\d,])(\d{1,3}(?:,\d{3})*,)(\d{1,2}) (\d{1,2})(?![\d,])",
        lambda m: m.group(1) + m.group(2) + m.group(3) if len(m.group(2) + m.group(3)) == 3 else m.group(0),
        text,
    )


def page_texts(pdf: bytes) -> list[str]:
    """The raw text of each page; fonts without a ToUnicode map are decoded with Adobe's tables (see cid_fonts.py)."""
    reader = PdfReader(io.BytesIO(pdf))
    add_missing_unicode_maps(reader)
    texts = [p.extract_text() or "" for p in reader.pages]
    if any(cn.TRADITIONAL_RE.search(t) for t in texts):  # typeset in traditional characters: convert the label characters
        texts = [re.sub(cn.TRADITIONAL_ENUMERATION_COMMA, "Ƚ", t).translate(cn.TO_SIMPLIFIED) for t in texts]
    return texts


def read_text(pdf: bytes) -> str:
    return clean_text(" ".join(page_texts(pdf)))


def to_number(token: str) -> float:
    token = token.strip()
    if token.startswith("("):
        return -float(token.strip("()").replace(",", ""))
    return float(token.replace(",", ""))


NOT_A_CHANGE = r"(?![\d.,])(?!\s*-?[\d,]+(?:\.\d+)?\s?%)"  # the whole last number, then no percentage


def find_row(
    text: str, name: str, labels: str | tuple[str, ...], count: int, start: int = 0, optional: str = "", end: str = "", earliest: bool = False
) -> re.Match:
    """The row of `count` numbers that follow a label (a footnote marker or note number may sit in between).

    `labels` is one pattern or several tried in order: a later one (a fallback) only where no earlier one is found; with
    `earliest`, the first row in the text whichever label it has.
    """
    found = []
    for label in (labels,) if isinstance(labels, str) else labels:
        m = re.compile(rf"(?:{label})\s*{optional}\s*{cn.NOTE}" + r"\s+".join([NUM] * count) + end).search(text, start)
        if m and not earliest:
            return m
        if m:
            found.append(m)
    if found:
        return min(found, key=lambda m: m.start())
    raise ValueError(f"Row not found in the report: {name}")


def row(
    text: str, name: str, labels: str | tuple[str, ...], count: int, start: int = 0, optional: str = "", end: str = "", earliest: bool = False
) -> tuple[list[float], int]:
    """The numbers of a row as printed, and the end position of the row in the text; raises if the row cannot be found."""
    m = find_row(text, name, labels, count, start, optional, end, earliest)
    return [to_number(g) for g in m.groups()], m.end()


def unit_at(text: str, pos: int, row_start: int | None = None) -> float:
    """The size of the amount unit in force at `pos`: the last unit statement before it (yuan: 1; thousand yuan: 1000).

    A row whose name (from `row_start` to `pos`) says yuan is in yuan.
    """
    if row_start is not None and cn.YUAN_LABEL_RE.search(text, row_start, pos):
        return 1.0
    last = None
    for last in cn.UNIT_RE.finditer(text, 0, pos):
        pass
    if last is None:
        return 1.0
    return next((size for size, group in zip(UNIT_SIZES, last.groups()) if group), 1.0)


def amount_row(text: str, name: str, labels: str | tuple[str, ...], count: int, start: int = 0, optional: str = "", end: str = "") -> tuple[list[float], int]:
    """Like row, but the amounts in yuan (or shares): banks state them in thousands."""
    m = find_row(text, name, labels, count, start, optional, end)
    unit = unit_at(text, m.start(1), m.start())
    return [to_number(g) * unit for g in m.groups()], m.end()


def balance_row(text: str, name: str, labels: tuple[str, ...]) -> tuple[list[float], int]:
    """A balance sheet row (yuan): this and the earlier balance sheet date.

    Not a row of a "major changes" table, which gives the end of the period and then a change: in percent (Innolight 2017
    Q3), or as a bare number with decimals after an amount without them (Bank of Nanjing 2016 Q3: "股本 6,058,720 80.00").
    """
    start = 0
    while True:
        m = find_row(text, name, labels, 2, start, end=NOT_A_CHANGE)
        first, second = m.group(1), m.group(2)
        if "." in second and "." not in first:
            start = m.start() + 1
            continue
        unit = unit_at(text, m.start(1), m.start())
        return [to_number(g) * unit for g in m.groups()], m.end()


def preference_and_perpetual(text: str, equity_at: int) -> list[float]:
    """Preference shares plus perpetual bonds (yuan, this and the earlier balance sheet date) in the equity section that ends at `equity_at`.

    They are rows under the other equity instruments, after the share capital. Where the other equity instruments are not
    broken down, all of them, if the report gives the equity of the ordinary shareholders separately (so it does not count
    them as theirs). A company without them gives [0, 0].
    """
    section_start = max(0, equity_at - 4000)
    section = text[section_start:equity_at]
    other = None
    for other in re.finditer(rf"{cn.OTHER_EQUITY_INSTRUMENTS}\s*{cn.NOTE}{NUM}\s+{NUM}", section):
        pass
    total = [0.0, 0.0]
    if other is None:
        return total
    dash = r"(?:[-–—－]|不适用)"
    broken_down = False
    for label in (cn.PREFERENCE_SHARES, cn.PERPETUAL_BONDS):
        m = re.compile(rf"{label}\s+({NUM[1:-1]}|{dash})\s+({NUM[1:-1]}|{dash})").search(section, other.end())
        if m:
            broken_down = True
            unit = unit_at(text, section_start + m.start(1))
            for i, g in enumerate(m.groups()):
                if not re.fullmatch(dash, g):
                    total[i] += to_number(g) * unit
    if not broken_down and cn.ORDINARY_SHAREHOLDERS_RE.search(text):
        unit = unit_at(text, section_start + other.start(1))
        total = [to_number(g) * unit for g in other.groups()]
    return total


def decimals(x: float) -> int:
    return len(f"{x:.6f}".rstrip("0").split(".")[1])


def rounded_alike(a: float, b: float) -> bool:
    """One EPS is the other rounded to two decimals (Innolight's 2019 income statement: 0.73; key table: 0.731)."""
    return min(decimals(a), decimals(b)) <= 2 and round(a, 2) == round(b, 2)


def statement_single_quarter(text: str, key: dict[str, float]) -> dict[str, float] | None:
    """The Q3 single-quarter figures (revenue and profit in millions) from an income statement with four columns (Q3 this
    year, Q3 last year, year-to-date this year and last year), when the key-data table gives only the year-to-date figures:
    its revenue and profit equal the third column. None otherwise (the key table's first column is the single quarter)."""
    try:
        try:
            revenue, pos = amount_row(text, "revenue", cn.INCOME_REVENUE, 4)
        except ValueError:
            revenue, pos = amount_row(text, "revenue", cn.INCOME_REVENUE_FALLBACK, 4, end=cn.INCOME_REVENUE_FALLBACK_NEXT)
        profit, pos = amount_row(text, "net profit", cn.INCOME_PROFIT, 4, pos, cn.LOSS_SIGN_NOTE)
        eps, _ = row(text, "basic EPS", (cn.INCOME_EPS, cn.INCOME_EPS_FALLBACK), 4, pos, earliest=True)
    except ValueError:
        return None
    if not all(abs(ytd - key[name]) <= 1e-6 * max(1.0, abs(ytd)) for name, ytd in (("revenue", revenue[2]), ("profit", profit[2]))):
        return None
    if abs(eps[2] - key["eps"]) > 0.0006:
        raise ValueError(f"basic EPS: the key table says {key['eps']} but the year-to-date column of the income statement says {eps[2]}")
    return {"revenue": revenue[0] / MILLION, "profit": profit[0] / MILLION, "eps": eps[0]}


def parse_cn_report(title: str, pdf: bytes) -> Parsed:
    m = REPORT_TITLE_RE.match(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    quarter = QUARTER_OF[m.group("kind")]
    period = pd.Period(year=int(m.group("year")), quarter=quarter, freq="Q")
    text = read_text(pdf)

    # Key accounting data table: the first column is the reporting period (the only source of the Q3 single quarter)
    key_start = text.find(cn.KEY_TABLE)
    if key_start < 0 and (fallback := re.search(cn.KEY_TABLE_FALLBACK, text)):
        key_start = fallback.start()
    if key_start < 0:
        raise ValueError("The key accounting data table was not found")
    key = {
        "revenue": amount_row(text, "revenue (key table)", (cn.KEY_REVENUE, cn.KEY_REVENUE_FALLBACK), 1, key_start, cn.FOOTNOTE)[0][0],
        "profit": amount_row(text, "net profit (key table)", (cn.KEY_PROFIT, cn.KEY_PROFIT_FALLBACK), 1, key_start, cn.FOOTNOTE)[0][0],
        "eps": row(text, "basic EPS (key table)", (cn.KEY_EPS, cn.KEY_EPS_FALLBACK), 1, key_start, cn.FOOTNOTE)[0][0],
    }

    # Consolidated statements: current and prior period (the prior period as restated in this report)
    try:
        total, pos = amount_row(text, "revenue", cn.INCOME_REVENUE, 2)
    except ValueError:
        total, pos = amount_row(text, "revenue", cn.INCOME_REVENUE_FALLBACK, 2, end=cn.INCOME_REVENUE_FALLBACK_NEXT)
    revenue = total
    operating = re.compile(rf"{cn.BEFORE_OPERATING_REVENUE}{cn.INCOME_OPERATING_REVENUE}\s*" + r"\s+".join([NUM] * 2)).match(text, pos)
    if operating:  # total operating revenue includes interest and fee income: use the operating revenue under it
        revenue, pos = [to_number(g) for g in operating.groups()], operating.end()
    profit, pos = amount_row(text, "net profit", cn.INCOME_PROFIT, 2, pos, cn.LOSS_SIGN_NOTE)
    eps, _ = row(text, "basic EPS", (cn.INCOME_EPS, cn.INCOME_EPS_FALLBACK), 2, pos, earliest=True)
    values = {"revenue": [v / MILLION for v in revenue], "profit": [v / MILLION for v in profit], "eps": eps}

    # Balance sheet rows, not the "major changes" table, which lists beginning and end and then a change in percent (Innolight 2017 Q3)
    equity, _ = balance_row(text, "equity", (cn.EQUITY, cn.EQUITY_FALLBACK))
    # PB is for the ordinary shareholders: preference shares and perpetual bonds (banks) are left out. They are listed in the
    # balance sheet, which is not always the first table with the equity total (an analysis table can come first)
    excluded = [0.0, 0.0]
    for total_row in re.finditer(rf"(?:{cn.EQUITY}|{cn.EQUITY_FALLBACK})\s*{cn.NOTE}{NUM}\s+{NUM}", text):
        excluded = preference_and_perpetual(text, total_row.start())
        if any(excluded):
            break
    if any(excluded):
        equity = [e - x for e, x in zip(equity, excluded)]
    share_capital, _ = balance_row(text, "share capital", (cn.SHARE_CAPITAL, cn.SHARE_CAPITAL_FALLBACK))
    values["equity"] = [equity[0] / MILLION, equity[1] / MILLION]
    values["wa_shares"] = [share_capital[0] / MILLION]  # period-end total shares (millions)

    quarterly = quarter in (1, 3)
    if quarter == 3:
        # The Q3 consolidated income statement is year-to-date, so the single quarter comes from the key table; unless the
        # key table is year-to-date as well and the statement has the single quarter first (Bank of Nanjing 2015, 2017)
        single = statement_single_quarter(text, key)
        values["revenue"] = [single["revenue"] if single else key["revenue"] / MILLION]
        values["profit"] = [single["profit"] if single else key["profit"] / MILLION]
        values["eps"] = [single["eps"] if single else key["eps"]]
    else:
        for name, scale in (("revenue", MILLION), ("profit", MILLION), ("eps", 1.0)):
            stated = key[name] / scale
            # Older key tables (Innolight) give the total operating revenue, which may include more than the operating revenue
            candidates = [values[name][0]] + ([total[0] / MILLION] if name == "revenue" else [])
            if any(abs(c - stated) <= (1e-6 if scale > 1 else 0.0006) * max(1.0, abs(c)) for c in candidates):
                continue
            if name == "eps" and rounded_alike(values["eps"][0], stated):
                values["eps"][0] = max(values["eps"][0], stated, key=decimals)  # keep the more precise of the two
                continue
            raise ValueError(f"{name}: the key table says {key[name]} but the consolidated income statement says {values[name][0] * scale}")
    return Parsed("CNY", values, period, quarterly)


def parse_cn_express(title: str, pdf: bytes) -> Parsed:
    """Express report: preliminary annual (or half-year) figures. The numbers are cumulative (current, prior-year period)."""
    m = EXPRESS_TITLE_RE.match(title)
    if not m:
        raise ValueError(f"Cannot tell the reporting period from the title: {title}")
    quarter = 2 if m.group("kind") == "H1" else 4
    period = pd.Period(year=int(m.group("year")), quarter=quarter, freq="Q")
    text = read_text(pdf)
    revenue_row = find_row(text, "revenue", (cn.EXPRESS_REVENUE, cn.EXPRESS_REVENUE_FALLBACK), 2, 0, cn.FOOTNOTE)
    revenue = [to_number(g) for g in revenue_row.groups()]
    # Amounts in yuan, in ten thousand yuan where the table says so (Innolight's 2025 annual express report), or in hundred
    # million yuan (banks); shares likewise
    before_table = text[: revenue_row.start()]
    unit = cn.EXPRESS_TEN_THOUSAND_RE.search(before_table)
    hundred_million = cn.EXPRESS_HUNDRED_MILLION_RE.search(before_table) is not None
    unit_size = TEN_THOUSAND if unit else HUNDRED_MILLION if hundred_million else 1.0
    profit, _ = row(text, "net profit", (cn.EXPRESS_PROFIT, cn.EXPRESS_PROFIT_FALLBACK), 2, 0, cn.FOOTNOTE)
    eps, _ = row(text, "basic EPS", cn.EXPRESS_EPS, 2, 0, cn.EXPRESS_EPS_UNIT + cn.FOOTNOTE + cn.EXPRESS_EPS_NOTE)
    equity, _ = row(text, "equity", (cn.EXPRESS_ORDINARY_EQUITY, cn.EXPRESS_EQUITY, cn.EXPRESS_EQUITY_FALLBACK), 2, 0, cn.FOOTNOTE)
    share_units = rf"(?:{cn.EXPRESS_TEN_THOUSAND_SHARES}|{cn.EXPRESS_HUNDRED_MILLION_SHARES})?"
    shares, _ = row(text, "share capital", cn.EXPRESS_SHARES, 2, 0, share_units + cn.FOOTNOTE)
    in_ten_thousands = re.search(rf"{cn.EXPRESS_SHARES}\s*{cn.EXPRESS_TEN_THOUSAND_SHARES}", text) is not None
    share_unit = TEN_THOUSAND if in_ten_thousands else HUNDRED_MILLION if hundred_million else 1.0
    values = {
        "revenue": [v * unit_size / MILLION for v in revenue],
        "profit": [v * unit_size / MILLION for v in profit],
        "eps": eps,
        "equity": [equity[0] * unit_size / MILLION, equity[1] * unit_size / MILLION],
        "wa_shares": [shares[0] * share_unit / MILLION],
    }
    return Parsed("CNY", values, period, quarterly=False)
