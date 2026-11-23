"""Extract earnings data from IFRS consolidated financial statements in HTML furnished on Form 6-K (XP Inc., in thousands of Brazilian reais).

XP's earnings press release has no basic EPS and only the total net income, so the program reads the financial statements that XP furnishes with it
(until 2021 as another exhibit of the 6-K with the press release, from 2022 on mostly in a 6-K of their own on the same day):

  interim (IAS 34)   "Interim condensed consolidated financial statements at September 30, 2026": an income statement with a three-months group
                     (this quarter, the same quarter last year) and, after the first quarter, a six- or nine-months group, in either order.
                     The balance sheet has one full date per column (the end of this quarter, the end of last year).
  annual (audited)   "Consolidated financial statements at December 31, 2025": full years only (three years in the income statement, two in the
                     balance sheet, whose columns are just years). The fourth quarter is then the full year minus the first nine months, worked out by the
                     caller from the cumulative figures (quarterly=False).

Rows: revenue is "Total revenue and income", net profit is the "Owners of the Parent company" row under "Net income attributable to:",
EPS is "Basic earnings per share" (R$ per share), the shares are the basic weighted average number of outstanding shares of the EPS note (thousands),
equity is "Equity attributable to owners of the Parent company" of the balance sheet. A table cut by a page break goes on without a header: it keeps
the columns of the table before it. Note numbers before the values are dropped by taking the last numbers of a row, and a few older notes write
EPS with a decimal comma ("1,6736"). The reporting period is the quarter of the newest balance sheet date.
The same value in two tables must agree and a missing value is an error: nothing is guessed.
"""

from __future__ import annotations

import re
from typing import NamedTuple

import pandas as pd

from valuation_monitor.parsed import Parsed

# Small table helpers, the same as in press_release.py and us_release.py but kept here, so that rule changes made there for other companies
# cannot change how XP's statements are read.
NUMBER_CELL_RE = re.compile(r"^\(?-?\d[\d,]*(?:\.\d+)?\)?$")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
MONTH_NUMBER = {**{m: i for i, m in enumerate(MONTHS, 1)}, **{m[:3]: i for i, m in enumerate(MONTHS, 1)}}  # "Sep" as well as "September"
FULL_DATE_RE = re.compile(r"^(?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2}), (?P<year>\d{4})$")


def clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").replace("’", "'").split())


def to_number(cell: str) -> float | None:
    """"1,234" -> 1234.0, "(1,234" or "(1,234)" -> -1234.0, "—" -> 0.0; None for anything else."""
    cell = "0" if cell == "—" else cell
    if not NUMBER_CELL_RE.match(cell):
        return None
    value = float(cell.strip("()").replace(",", ""))
    return -value if cell.startswith("(") else value


def table_rows(table) -> list[list[str]]:
    """The non-empty cells of each row of an HTML table (lone ")", "%" and "$" cells dropped)."""
    rows = []
    for tr in table.find_all("tr"):
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        rows.append([c for c in cells if c and c not in (")", "%", ")%", "$")])
    return [r for r in rows if r]


def header_cell(cell: str) -> str:
    """A header cell without its footnote mark: "2019*" and "December 31, 2016 (1)" become "2019" and "December 31, 2016"."""
    return re.sub(r"\s*(?:\([\da-z]\)|\*+|†)+$", "", cell).strip()

GROUP_RE = re.compile(r"\b(?P<kind>three|six|nine|twelve)[- ]months? periods? ended\b|\b(?P<year>years?) ended\b", re.I)
YEAR_CELL_RE = re.compile(r"^((?:19|20)\d\d)$")
UNIT_RE = re.compile(r"in (thousands|millions) of (?:Brazilian )?Rea(?:is|ls)", re.I)
INTERIM_RE = re.compile(r"\binterim\b", re.I)
REVENUE_RE = re.compile(r"^total revenue and income$", re.I)
OWNERS_RE = re.compile(r"^owners of the parent(?: company)?$", re.I)
EPS_RE = re.compile(r"^basic earnings per share(?: - R\$)?$", re.I)
SHARES_RE = re.compile(r"^basic weighted average (?:number|quantity) of (?:outstanding )?(?:ordinary )?shares\b", re.I)  # 2020: "quantity of shares"
EQUITY_RE = re.compile(r"^equity attributable to (?:the )?owners of the parent(?: company)?$", re.I)
NOTE_MARK_RE = re.compile(r"(?:\s*\((?:[ivx]+|\d)\))+$")  # "(i) (iii)" or "(i)(iii)" after a label
DECIMAL_COMMA_RE = re.compile(r"^\(?-?\d+,\d{4}\)?$")  # "1,6736": four digits after the comma cannot be a thousands separator
KIND_MONTHS = {"three": 3, "six": 6, "nine": 9, "twelve": 12}
AMOUNT_KEYS = ("revenue", "profit")
SHARES_SCALE = 0.001  # the EPS note gives the shares in thousands
TOLERANCE = 0.001


class Columns(NamedTuple):
    years: list[int]  # the year of each value column
    months: list[int]  # the length in months of the period of each value column (3, 6, 9 or 12)


def label_of(row: list[str]) -> str:
    return NOTE_MARK_RE.sub("", row[0]).strip()


def numbers(row: list[str], per_share: bool = False) -> list[float]:
    out = []
    for cell in row[1:]:
        if per_share and DECIMAL_COMMA_RE.match(cell):
            cell = cell.replace(",", ".")
        if (v := to_number(cell)) is not None:
            out.append(v)
    return out


def full_date(cell: str) -> pd.Timestamp | None:
    m = FULL_DATE_RE.match(header_cell(cell))
    if not m or m.group("month") not in MONTH_NUMBER:
        return None
    try:
        return pd.Timestamp(year=int(m.group("year")), month=MONTH_NUMBER[m.group("month")], day=int(m.group("day")))
    except ValueError:  # a typing error such as "September 31, 2023" in a header
        return None


def year_header(rows: list[list[str]]) -> tuple[int, list[int]] | None:
    """(index of the header row, the year of each value column): the first row whose cells (after "Note", and after a heading such as
    "Liabilities and equity" in the first cell) are all years."""
    for i, row in enumerate(rows[:8]):
        cells = [header_cell(c) for c in row if header_cell(c).lower() != "note"]
        if cells and not YEAR_CELL_RE.match(cells[0]):
            cells = cells[1:]
        years = [int(m.group(1)) for c in cells if (m := YEAR_CELL_RE.match(c))]
        if len(years) >= 2 and len(years) == len(cells):
            return i, years
    return None


def columns_of(rows: list[list[str]], annual: bool) -> tuple[int, Columns] | None:
    """The value columns of an income statement or EPS note: groups ("Nine months periods ended September 30," "Three months period ended ...")
    above the years, or no group at all (a first quarter or annual statements: one period length for the whole document)."""
    found = year_header(rows)
    if found is None:
        return None
    header, years = found
    text = " ".join(c for r in rows[:header] for c in r)
    kinds = [KIND_MONTHS[m.group("kind").lower()] if m.group("kind") else 12 for m in GROUP_RE.finditer(text)] or [12 if annual else 3]
    if len(years) % len(kinds):
        return None
    n = len(years) // len(kinds)
    return header + 1, Columns(years, [k for k in kinds for _ in range(n)])


def scan_rows(rows: list[list[str]], columns: Columns, found: dict[str, list[float]], section: str) -> str:
    """Pick the revenue, profit, EPS and shares rows of a table; `section` carries "Net income attributable to:" over a page break."""
    for row in rows:
        label = label_of(row)
        low = label.lower()
        per_share = bool(EPS_RE.match(label))
        nums = numbers(row, per_share)
        if not nums:
            if re.match(r"^net income (?:\(loss\) )?attributable to", low):
                section = "profit"
            elif "attributable to" in low:
                section = ""
            continue
        if len(nums) < len(columns.years):
            continue
        mine = nums[-len(columns.years) :]
        if REVENUE_RE.match(label):
            found.setdefault("revenue", mine)
        elif OWNERS_RE.match(label) and section == "profit":
            found.setdefault("profit", mine)
            section = ""
        elif per_share:
            found.setdefault("eps", mine)
        elif SHARES_RE.match(label):
            found.setdefault("wa_shares", mine)
    return section


def balance_dates(rows: list[list[str]], annual: bool) -> list[pd.Timestamp] | None:
    """One full date per column ("September 30, 2026", "December 31, 2025"); annual balance sheets have only the years (at December 31)."""
    for row in rows[:8]:
        dates = [d for c in row if (d := full_date(c))]
        if len(dates) >= 2:
            return dates
    if annual and (found := year_header(rows)) is not None:
        return [pd.Timestamp(year=y, month=12, day=31) for y in found[1]]
    return None


def parse_balance(rows: list[list[str]], annual: bool) -> tuple[pd.Timestamp, list[float]] | None:
    """Equity attributable to owners: (the newest date, [end of this period, end of last year])."""
    row = next((r for r in rows if EQUITY_RE.match(label_of(r)) and numbers(r)), None)
    if row is None:
        return None
    dates = balance_dates(rows, annual)
    if not dates or len(numbers(row)) < len(dates):
        return None
    nums = numbers(row)[-len(dates) :]
    order = sorted(range(len(dates)), key=lambda c: dates[c], reverse=True)
    return dates[order[0]], [nums[order[0]], nums[order[1]]]


def close(a: float, b: float, key: str) -> bool:
    return abs(a - b) <= (0.00005 if key == "eps" else max(1.0, abs(a) * TOLERANCE))


def pick(found: dict[str, list[float]], columns: Columns, period: pd.Period, months: int) -> dict[str, list[float]]:
    """The values of this period and of the same period last year (the group of `months` months)."""
    out = {}
    for key, nums in found.items():
        by_year = {y: v for y, m, v in zip(columns.years, columns.months, nums) if m == months}
        if period.year in by_year and period.year - 1 in by_year:
            out[key] = [by_year[period.year], by_year[period.year - 1]]
    return out


def parse_ifrs_statements(html: bytes) -> Parsed:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    text = clean(soup.get_text(" "))
    unit = UNIT_RE.search(text)
    if unit is None:
        raise ValueError("Cannot find the unit of the amounts (in thousands of Brazilian Reais)")
    scale = 0.001 if unit.group(1).lower() == "thousands" else 1.0
    annual = INTERIM_RE.search(text[:3000]) is None  # the heading of interim statements says so; the audited annual statements do not

    tables: list[tuple[Columns, dict[str, list[float]]]] = []  # the values found under each set of columns
    balances: list[tuple[pd.Timestamp, list[float]]] = []
    previous: Columns | None = None  # the columns of the table before, for a table cut by a page break
    section = ""
    for table in soup.find_all("table"):
        rows = table_rows(table)
        if not rows:
            continue
        if (equity := parse_balance(rows, annual)) is not None:
            balances.append(equity)
        found_columns = columns_of(rows, annual)
        if found_columns is not None:
            first, columns = found_columns
            section = ""
        elif previous is not None:
            first, columns = 0, previous
        else:
            continue
        found: dict[str, list[float]] = {}
        section = scan_rows(rows[first:], columns, found, section)
        if found:
            tables.append((columns, found))
        previous = columns if found_columns is not None or found else None
    if not balances:
        raise ValueError("Not found: the balance sheet (equity attributable to owners of the Parent company)")
    end, equity = balances[0]
    for other_end, other in balances[1:]:
        if other_end == end and any(not close(a, b, "equity") for a, b in zip(other, equity)):
            raise ValueError(f"The balance sheets in these statements disagree: {equity} and {other}")
    period = pd.Period(end, freq="Q")
    if annual and period.quarter != 4:
        raise ValueError(f"Annual statements for {period}")
    months = 12 if annual else 3

    values: dict[str, list[float]] = {}
    for columns, found in tables:
        for key, pair in pick(found, columns, period, months).items():
            if key not in values:
                values[key] = pair
            elif any(not close(a, b, key) for a, b in zip(pair, values[key])):
                raise ValueError(f"{key} differs between tables: {values[key]} and {pair}")
    missing = {"revenue", "profit", "eps"} - values.keys()
    if missing:
        raise ValueError(f"Not found: {sorted(missing)} for {months} months to {end:%Y-%m-%d} and the same period last year")
    for key in AMOUNT_KEYS:
        if key in values:
            values[key] = [v * scale for v in values[key]]  # thousands of reais -> millions
    if "wa_shares" in values:
        values["wa_shares"] = [v * SHARES_SCALE for v in values["wa_shares"]]  # thousands of shares -> millions
    values["equity"] = [v * scale for v in equity]
    return Parsed("BRL", values, period, not annual)
