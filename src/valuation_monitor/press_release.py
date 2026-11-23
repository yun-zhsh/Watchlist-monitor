"""Extract earnings data from the earnings press releases (HTML) of US-listed companies (verified on all JD press releases from August 2014 on
and all Alibaba Group releases from January 2015 on).

The tables in the press release are laid out as "header + data rows":
  the first header row describes each group (For the three months ended / six months ended / year ended),
  followed by a date row and a currency row (RMB, RMB, US$); each group ends with a US dollar column.
  Some companies (Alibaba) write only the years in the date row; the month and day are then in the group description
  ("Three months ended June 30," over "2024 2025") or in one description per column ("As of March 31," "As of June 30,").
  The consolidated income statement has a quarter group (and a year-to-date group); the "three months" group is needed, whose columns are told apart by date:
  this quarter, same quarter last year, and in some years the previous quarter.
  Revenue is Total net revenues (Alibaba: Revenue), net profit is the row attributable to ordinary shareholders, EPS is basic EPS "per ADS" (RMB),
  or basic EPS per share when the release has no per-ADS figures (Alibaba until the 2019 share subdivision: one ADS was one share);
  the shares per ADS are the per-ADS basic EPS divided by the per-share basic EPS of the same release (JD 2; Alibaba 1 until 2019, then 8),
  Non-GAAP EPS is the basic Non-GAAP EPS per ADS; from the balance sheet the equity attributable to the company's shareholders (end of this period, end of last year) is taken.
Units (thousands or millions) are stated in the note before the table and are all converted to millions.
When the same value appears in several tables it must agree, otherwise an error is raised; one of them is never silently picked.
"""

from __future__ import annotations

import re
from typing import NamedTuple

import pandas as pd

from valuation_monitor.parsed import Parsed

DATE_RE = re.compile(r"^([A-Z][a-z]+) ?(\d{1,2}), (\d{4})$")  # a few press releases write "September30" without the space
CURRENCY_CELL_RE = re.compile(r"^(RMB|US\$|USD|HK\$)$")
NUMBER_CELL_RE = re.compile(r"^\(?-?\d[\d,]*(?:\.\d+)?\)?$")
UNIT_RE = re.compile(r"in (thousands|millions)", re.I)
GROUP_RE = re.compile(r"(three|six|nine|twelve) months ended|year ended|as of", re.I)
# Total net profit attributable to ordinary shareholders. In years with discontinued operations there are also "continuing" and "discontinued" sub-rows, which must not be taken
PROFIT_LABEL_RE = re.compile(r"^net (?!.*(?:continuing|discontinued|non-?controlling|non-gaap)).*attributable to", re.I)
ORDINARY_PROFIT_RE = re.compile(r"attributable to (?:the company's )?ordinary shareholders", re.I)  # preferred over a row attributable to the company itself (Alibaba: before the accretion of mezzanine equity)
# Equity attributable to the company: "Total JD.com, Inc. shareholders' equity", "Total Alibaba Group Holding Limited shareholders' equity"
EQUITY_LABEL_RE = re.compile(r"^total (?!liabilities|shareholders?'?s? equity)\S.* shareholders.? equity", re.I)
TOTAL_EQUITY_LABEL_RE = re.compile(r"^total shareholders?'?s? equity$", re.I)
EPS_SECTION_RE = re.compile(r"^(?:\(loss\) )?(?:net|earnings|loss) ", re.I)  # "Net income per ADS:", "Earnings per ADS attributable to ...", "(Loss) Earnings per ADS ..."
YEAR_CELL_RE = re.compile(r"^\d{4}$")
LABEL_DAY_RE = re.compile(r"\b([A-Z][a-z]+) ?(\d{1,2}),?$")  # the month and day at the end of a group description, e.g. "Three months ended June 30,"
SPACED_NUMBER_RE = re.compile(r"^\(? ?-?\d[\d, ]*(?:\.[\d ]+)?\)?$")  # a number broken by stray spaces (Alibaba 2015-2016: "2,86 9", "(6, 133", "( 2")
ADS_RATIO_TOLERANCE = 0.05  # allowed relative distance of (EPS per ADS / EPS per share) from a whole number
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
AMOUNT_KEYS = ("revenue", "profit", "wa_shares")  # amount rows, converted to millions by the unit of the table
TOLERANCE = 0.001  # allowed (relative) difference of the same value in different tables


def clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").replace("’", "'").split())


def to_number(cell: str) -> float | None:
    cell = cell.replace("—", "0") if cell == "—" else cell
    if not NUMBER_CELL_RE.match(cell):
        if " " not in cell or not SPACED_NUMBER_RE.match(cell):
            return None
        cell = cell.replace(" ", "")
        if not NUMBER_CELL_RE.match(cell):
            return None
    value = float(cell.strip("()").replace(",", ""))
    return -value if cell.startswith("(") else value


def table_rows(table) -> list[list[str]]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        rows.append([c for c in cells if c and c not in (")", "%", ")%", "$")])
    return [r for r in rows if r]


def parse_date(cell: str) -> pd.Timestamp | None:
    m = DATE_RE.match(cell)
    if not m or m.group(1) not in MONTHS:
        return None
    return pd.Timestamp(year=int(m.group(3)), month=MONTHS[m.group(1)], day=int(m.group(2)))


class Header(NamedTuple):
    dates: list[pd.Timestamp]
    currencies: list[str]
    groups: list[list[int]]  # column numbers of each group
    labels: list[str]  # description of each group (may be empty)
    first_data_row: int
    inherited: bool = False  # the header of the previous table is reused


def currency_groups(currencies: list[str]) -> list[list[int]]:
    groups, current = [], []
    for j, ccy in enumerate(currencies):
        current.append(j)
        if ccy != "RMB":  # each group ends with a US dollar column
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def label_day(label: str, year: int) -> pd.Timestamp | None:
    m = LABEL_DAY_RE.search(label)
    if not m or m.group(1) not in MONTHS:
        return None
    return pd.Timestamp(year=year, month=MONTHS[m.group(1)], day=int(m.group(2)))


def dates_from_years(years: list[int], currencies: list[str], labels: list[str]) -> list[pd.Timestamp] | None:
    """A date row with years only: one year per RMB column; the month and day come from the group descriptions (one per group or one per RMB column).
    A US dollar column (the convenience translation) takes the date of the RMB column before it."""
    groups = currency_groups(currencies)
    rmb = [j for j, ccy in enumerate(currencies) if ccy == "RMB"]
    if len(years) != len(rmb):
        return None
    if len(labels) == len(groups):
        label_of = {j: labels[g] for g, cols in enumerate(groups) for j in cols}
    elif len(labels) == len(rmb):
        label_of = {j: labels[k] for k, j in enumerate(rmb)}
    else:
        return None
    dates: list[pd.Timestamp | None] = []
    year_of = dict(zip(rmb, years))
    for j, ccy in enumerate(currencies):
        if ccy == "RMB":
            dates.append(label_day(label_of[j], year_of[j]))
        else:
            dates.append(dates[-1] if dates and j - 1 in year_of else None)
    return None if any(d is None for d in dates) else dates


def parse_header(rows: list[list[str]]) -> Header | None:
    years: list[int] | None = None
    for i, row in enumerate(rows[:8]):
        dates = [parse_date(c) for c in row]
        if len(row) >= 2 and all(d is not None for d in dates):
            break
        if i > 0 and len(row) >= 2 and all(YEAR_CELL_RE.match(c) for c in row):
            years = [int(c) for c in row]
            break
    else:
        return None
    if i + 1 >= len(rows) or not all(CURRENCY_CELL_RE.match(c) for c in rows[i + 1]):
        return None
    currencies = rows[i + 1]
    groups = currency_groups(currencies)
    labels = [c for r in rows[:i] for c in r if GROUP_RE.search(c)]
    if years is not None:
        dates = dates_from_years(years, currencies, labels)
        if dates is None:
            return None
    elif len(currencies) != len(dates):
        return None
    return Header(dates, currencies, groups, labels if len(labels) == len(groups) else [""] * len(groups), i + 2)


def unit_scale(table, rows: list[list[str]]) -> float:
    """Factor converting amounts to millions. The unit is in a row of the table or in the note before the table."""
    for r in rows[:6]:
        if len(set(r)) == 1 and (m := UNIT_RE.search(r[0])):  # some unit rows repeat the same note over several cells
            return 0.001 if m.group(1).lower() == "thousands" else 1.0
    for s in table.find_all_previous(string=True, limit=40):
        if m := UNIT_RE.search(clean(s)):
            return 0.001 if m.group(1).lower() == "thousands" else 1.0
    raise ValueError("Cannot find the unit of the amounts")


def columns_by_date(header: Header, group: int, values: list[float]) -> dict[pd.Timestamp, float]:
    """In a row of values (numbers only, no label), the values of the RMB columns, indexed by date."""
    extra = 0 if header.inherited else 1  # in a table with its own header, the one extra number is the year-over-year percentage
    if not len(header.dates) <= len(values) <= len(header.dates) + extra:
        return {}
    out = {}
    for col in header.groups[group]:
        if header.currencies[col] == "RMB":
            out[header.dates[col]] = values[col]
    return out


def leading_columns(header: Header, group: int, values: list[float]) -> dict[pd.Timestamp, float]:
    """Numbers in the share row: only the RMB columns of the first group (three months) are matched by date; the US dollar columns of this row are blank in some years and repeated in others, so only the first few numbers are taken."""
    if group != 0:
        return {}
    rmb = [c for c in header.groups[0] if header.currencies[c] == "RMB"]
    if len(values) < len(rmb):
        return {}
    return {header.dates[c]: values[k] for k, c in enumerate(rmb)}


def numbers(row: list[str]) -> list[float]:
    return [v for c in row[1:] if (v := to_number(c)) is not None]


def quarter_offsets(by_date: dict[pd.Timestamp, float]) -> tuple[pd.Period, list[float]]:
    """Arrange by date as [this quarter, same quarter last year, (previous quarter)]."""
    periods = {pd.Period(d, freq="Q"): v for d, v in by_date.items()}
    current = max(periods)
    out = [periods.get(current), periods.get(current - 4), periods.get(current - 1)]
    if out[0] is None or out[1] is None:
        raise ValueError(f"The quarterly data lacks the current or the prior-year quarter: {sorted(periods)}")
    return current, out[:2] if out[2] is None else out


def parse_flow_rows(rows, header: Header, scale: float | None) -> dict[str, tuple[pd.Period, list[float]]]:
    """Revenue, net profit, basic EPS per ADS and basic Non-GAAP EPS in the "three months" group.

    The consolidated income statement may be split by page breaks into several tables, so each table only takes the rows it has, and the caller merges them and cross-checks.
    """
    group = next((g for g, label in enumerate(header.labels) if re.search("three months", label, re.I)), None)
    if group is None:
        return {}
    found: dict[str, dict[pd.Timestamp, float]] = {}
    section = None  # which "per ADS" (or "per share") section we are in: eps, non_ifrs_eps or eps_share
    basic = False  # whether we are under the "Basic" part of the section (in years with discontinued operations, Basic is further split into continuing, discontinued and total)
    ordinary_profit = False  # whether the profit row found is the one attributable to ordinary shareholders
    for row in rows[header.first_data_row :]:
        label = row[0]
        nums = numbers(row)
        low = label.lower()
        if not nums:
            if re.match(r"weighted average (?:number of )?shares?\b", low):  # the weighted average shares used for basic EPS (thousands or millions)
                section = "wa_shares"
                continue
            if "per ads" in low:
                section = "non_ifrs_eps" if re.search("non-gaap|adjusted", low) else "eps" if EPS_SECTION_RE.match(low) else None
                basic = False
            elif "per share" in low and EPS_SECTION_RE.match(low) and not re.search("non-gaap|adjusted", low):
                section = "eps_share"  # used for the shares per ADS, and as the EPS when there are no per-ADS figures
                basic = False
            elif low == "basic":
                basic = True
            elif low == "diluted":
                basic = False
            else:
                section = None
            continue
        if section == "wa_shares" and low == "basic" and "wa_shares" not in found:
            found["wa_shares"] = leading_columns(header, group, nums)
        elif section and section not in found and section != "wa_shares" and (low == "basic" or (basic and re.match(r"net (income|loss).*per (ads|share)", low))):
            found[section] = columns_by_date(header, group, nums)
        elif re.match(r"total net revenues|revenues?$", low) and "revenue" not in found:
            found["revenue"] = columns_by_date(header, group, nums)
        elif PROFIT_LABEL_RE.match(label) and ("profit" not in found or (ORDINARY_PROFIT_RE.search(label) and not ordinary_profit)):
            found["profit"] = columns_by_date(header, group, nums)
            ordinary_profit = bool(ORDINARY_PROFIT_RE.search(label))
    out = {}
    for key, by_date in found.items():
        if not by_date:
            continue
        period, vals = quarter_offsets(by_date)
        if key in AMOUNT_KEYS:
            if scale is None:
                continue  # tables with an unknown unit are not used; the same numbers are in other tables
            vals = [v * scale for v in vals]  # shares stay in millions of ordinary shares here; parse_press_release divides by the shares per ADS
        out[key] = (period, vals)
    return out


def shares_per_ads(eps_ads: list[float] | None, eps_share: list[float] | None) -> int:
    """Ordinary shares per ADS: basic EPS per ADS / basic EPS per share of the same release, taken on the largest figure (least rounding).
    Without per-ADS figures (Alibaba until 2019) one ADS is one share and the per-share EPS is used."""
    if eps_ads is None or eps_share is None:
        return 1
    k = max(range(len(eps_share)), key=lambda i: abs(eps_share[i]))
    if eps_share[k] == 0:
        raise ValueError("Cannot derive the shares per ADS: the basic EPS per share is zero")
    ratio = eps_ads[k] / eps_share[k]
    whole = round(ratio)
    if whole < 1 or abs(ratio - whole) > ADS_RATIO_TOLERANCE * whole:
        raise ValueError(f"The basic EPS per ADS / per share is not a whole number of shares: {eps_ads} / {eps_share}")
    return whole


def parse_balance_table(rows, header: Header, scale: float | None) -> tuple[pd.Period, list[float]] | None:
    """Balance sheet: equity attributable to the company's shareholders, for the end of this period and the end of last year (December 31)."""
    labels = [r[0] for r in rows]
    if not any(re.match(r"total (liabilities|assets)", label, re.I) for label in labels):
        return None
    if scale is None:
        return None
    # "Total shareholders' equity" includes the non-controlling interests when they are listed above it (JD); when a "Total equity" row
    # follows (Alibaba: "Noncontrolling interests" between the two), it is the equity attributable to the company
    has_nci = any(re.match(r"non-?controlling interests?$", label, re.I) for label in labels) and not any(re.match(r"total equity$", label, re.I) for label in labels)
    pattern = EQUITY_LABEL_RE if has_nci else re.compile(f"{EQUITY_LABEL_RE.pattern}|{TOTAL_EQUITY_LABEL_RE.pattern}", re.I)
    for row in rows[header.first_data_row :]:
        if pattern.match(row[0]):
            by_date = columns_by_date(header, 0, numbers(row))
            if len(by_date) < 2:
                return None
            current = max(by_date)
            earlier = max(d for d in by_date if d != current)
            if earlier != pd.Timestamp(year=current.year - 1, month=12, day=31):
                # the comparison column is the end of a fiscal year that is not the calendar year (Alibaba: March 31), while the history files the second
                # balance under the previous December quarter; only this period's balance is kept (the earlier one is in that period's own release)
                return pd.Period(current, freq="Q"), [by_date[current] * scale]
            return pd.Period(current, freq="Q"), [by_date[current] * scale, by_date[earlier] * scale]
    return None


def close(a: float, b: float, key: str) -> bool:
    return abs(a - b) <= (0.005 if key in ("eps", "non_ifrs_eps", "eps_share") else max(1.0, abs(a) * TOLERANCE))


def parse_press_release(html: bytes) -> Parsed:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    candidates: dict[str, list[tuple[pd.Period, list[float]]]] = {}
    previous = None  # header of the immediately preceding table (None when that table has no header of its own)
    for table in soup.find_all("table"):
        rows = table_rows(table)
        header = parse_header(rows)
        own, header = header, header or (previous and previous._replace(first_data_row=0, inherited=True))
        previous = own
        if header is None:
            continue
        try:
            scale = unit_scale(table, rows)
        except ValueError:
            scale = None
        for key, found in parse_flow_rows(rows, header, scale).items():
            if header.inherited and key not in ("eps", "non_ifrs_eps", "eps_share"):
                continue  # when the income statement is split across pages, the second half (per-share data) has no header and reuses the previous one; amounts only count in tables with their own header
            candidates.setdefault(key, []).append(found)
        if not header.inherited and (equity := parse_balance_table(rows, header, scale)) is not None:
            candidates.setdefault("equity", []).append(equity)

    if "eps" not in candidates and "eps_share" in candidates:
        candidates["eps"] = candidates.pop("eps_share")  # no per-ADS figures: one ADS is one share
    missing = {"revenue", "profit", "eps", "equity"} - candidates.keys()
    if missing:
        raise ValueError(f"Not found: {sorted(missing)}")
    values: dict[str, list[float]] = {}
    periods = set()
    for key, found in candidates.items():
        (period, first), *others = found
        for p, other in others:
            if p != period or any(not close(a, b, key) for a, b in zip(other, first)):
                raise ValueError(f"{key} differs between tables: {found}")
        values[key] = first
        periods.add(period)
    if len(periods) != 1:
        raise ValueError(f"The rows disagree on the reporting period: {periods}")
    ratio = shares_per_ads(values.get("eps"), values.pop("eps_share", None))
    if "wa_shares" in values:
        values["wa_shares"] = [v / ratio for v in values["wa_shares"]]  # millions of shares -> millions of ADS
    return Parsed("CNY", values, periods.pop())
