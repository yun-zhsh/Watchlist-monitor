"""Extract earnings data from condensed US GAAP income statements and balance sheets in HTML: the earnings press releases of US companies
(exhibit 99.1 of Form 8-K; Meta Platforms, Cognizant, Amphenol, Dynatrace), the 6-K press releases of Sea Limited, and the US GAAP summary financial statements that ASML
furnishes with each quarterly 6-K (in euros).

The document holds a condensed income statement and a condensed balance sheet as tables:

  income statement   a header with the groups ("Three Months Ended September 30," and "Nine Months Ended September 30," or "Year Ended ...") and one
                     column per year, then rows: revenue, net income, basic earnings per share and the basic weighted-average shares.
                     The "three months" group has this quarter and the same quarter last year. A release without one (Sea's second and third
                     quarters: six and nine months only) gives its year-to-date group instead (Parsed.quarterly is False).
                     With non-controlling interests the net income attributable to the company's shareholders is taken, not the total.
  balance sheet      two columns, the end of this quarter and the end of last year (only the first when last year's column is not December:
                     Dynatrace's fiscal year ends March 31); the row "Total stockholders' equity", or the company's own
                     shareholders' equity when there are non-controlling interests ("Total Sea Limited shareholders' equity").

  Non-GAAP EPS       only for a stock with a Non-GAAP PE (parse_us_release's non_gaap): on basic EPS, the basic row of a GAAP to Non-GAAP
                     reconciliation with the same kind of header ("Non-GAAP net income per share - basic", Dynatrace; optional per release),
                     this period and the same period last year, as published; on diluted EPS, Amphenol's Adjusted Diluted EPS.

Amounts are converted to millions (the unit is in the table or the line before it), shares are in millions (Sea: single shares when the unit note
says "except for number of shares", thousands when the label says so), per-share data is as published.
ASML's fiscal quarters end on a Sunday near the calendar quarter end (for example July 3, 2022), so a date belongs to the quarter ten days before it; its dates
are one per column ("Jul 4," ... "Jul 3,") and its newest releases (2025 on) have no tables at all, only the text of the statements (parse_text).
The same value in two tables must agree and a missing value is an error: nothing is guessed.
"""

from __future__ import annotations

import re
from typing import NamedTuple

import pandas as pd

from valuation_monitor.parsed import Parsed
from valuation_monitor.press_release import MONTHS, UNIT_RE, clean, parse_date, table_rows, to_number

GROUP_RE = re.compile(r"^(?P<kind>three|six|nine|twelve) months ended (?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2}),?$|^(?:full )?(?P<year_kind>year) ended (?P<ymonth>[A-Z][a-z]+)\.? (?P<yday>\d{1,2}),?$", re.I)
KIND_RE = re.compile(r"^(?P<kind>three|six|nine|twelve) months ended,?$|^(?P<year>year) ended,?$", re.I)  # older releases put the date in the next row
DAY_RE = re.compile(r"^(?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2}),?$")
FULL_DATE_RE = re.compile(r"^(?P<month>[A-Z][a-z]+)\.? (?P<day>\d{1,2}), (?P<year>\d{4})$")
MONTH_NUMBER = {**MONTHS, **{name[:3]: number for name, number in MONTHS.items()}}  # "Sep" as well as "September"
YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
REVENUE_RE = re.compile(r"^(?:total )?(?:net )?(?:revenues?|sales)$", re.I)
# the owners' row, not its per-share rows: "Net income attributable to Amphenol Corporation per common share — Basic" and
# "Net income attributable to Amphenol Corporation - Basic" are EPS (BASIC_EPS_RE)
PROFIT_RE = re.compile(r"^net (?:\((?:loss|income)\) )?(?:income|loss)(?: \((?:loss|income)\))?(?P<owners> attributable to (?!.*non-?controlling)(?!.*\bper (?:common |ordinary )?share\b)(?!.* ?[-–—] ?(?:basic|diluted)\b).*)?$", re.I)
BASIC_EPS_RE = re.compile(
    r"^basic (?:\((?:loss|losses)\) )?(?:earnings|net income)(?: \((?:loss|losses)\))? per (?:common |ordinary )?share$"
    # Amphenol: "Net income per common share - Basic" ("Net income (loss) ..." in 2017-2018), "Net income attributable to Amphenol Corporation per common share — Basic", and in 2021-2022
    # (with discontinued operations) "Net income attributable to Amphenol Corporation - Basic" under a heading "Net income per common share ... — Basic:"
    r"|^net income(?: \(loss\))?(?: attributable to (?!.*non-?controlling).+?)?(?: per (?:common |ordinary )?share)? ?[-–—] ?basic$",
    re.I,
)
BASIC_SHARES_RE = re.compile(r"^weighted[- ]average (?:number of )?(?:common |ordinary )?shares.*\bbasic\b", re.I)
# Dynatrace's reconciliation of GAAP to Non-GAAP net income: "Non-GAAP net income per share - basic" (also "Non-GAAP net (loss) income per share - basic");
# the diluted row and the summary table's "Non-GAAP net income per share" (diluted shares) are not taken
NON_GAAP_BASIC_EPS_RE = re.compile(r"^non-gaap net (?:\((?:loss|income)\) )?(?:income|loss)(?: \((?:loss|income)\))? per (?:common |ordinary )?share\s*-\s*basic$", re.I)
SHARES_SECTION_RE = re.compile(r"^(?:weighted[- ]average|shares used in)", re.I)  # Sea before 2019: "Shares used in loss per share computation:"
BASIC_ROW_RE = re.compile(r"^basic(?: and diluted)?$", re.I)  # in a loss year Sea has one row "Basic and diluted"
# Sea: "shareholders' (deficit) equity"; Dynatrace was an LLC until its 2019 IPO: "Total member's deficit", then "Total shareholders' equity / member's deficit"
EQUITY_WORDS = r"(?:(?:stockholders|shareholders).?|member.?s) (?:\((?:deficit|equity)\) )?(?:equity|deficit)(?: \((?:deficit|equity)\))?(?: / member.?s? (?:deficit|equity))?$"
EQUITY_RE = re.compile(rf"^(?:total )?{EQUITY_WORDS}", re.I)
# the equity of the company's own shareholders, when the total also has non-controlling interests: "Total Sea Limited shareholders' equity (deficit)"
# or the total attributable to the company, with the non-controlling interests below it: "Total stockholders' equity attributable to Amphenol Corporation"
OWNERS_EQUITY_RE = re.compile(rf"^total (?!liabilities)\S.*? {EQUITY_WORDS}|^total (?:stockholders|shareholders).? equity attributable to (?!.*non-?controlling)\S.*$", re.I)
SHARES_UNIT_RE = re.compile(r"\(in (thousands|millions)\)", re.I)  # a unit in the label of the share rows: "computation (in thousands):"
RAW_SHARES_RE = re.compile(r"except (?:for )?(?:the )?number of shares", re.I)  # "in thousands of US dollars except for number of shares & per share data"
# a unit for dollars only leaves the share counts in single shares: Amphenol until 2015 "(dollars in millions, except per share data)" (later "(dollars and
# shares in millions, ...)"), Dynatrace's first release (September 2019) "(Unaudited - dollars in thousands, except per share amounts)" with 237,693,127 shares
DOLLARS_ONLY_RE = re.compile(r"^\(?(?:unaudited\s*[-\u2013\u2014]\s*)?dollars in (?:thousands|millions)\b", re.I)
PREFIX_RE = re.compile(r"^(?:for the|as of)\s+", re.I)  # Sea: "For the Three Months ended September 30,", "As of December 31,"
TOLERANCE = 0.001
NOTE_SUFFIX_RE = re.compile(r"\s+\d(?:,\d)*$")  # footnote numbers after a label: "Diluted net income per ordinary share 4"


def unit_note(table, rows: list[list[str]]) -> str | None:
    """The text that states the unit of a table: a cell of its first rows or a line before it; None if the unit is not stated."""
    for row in rows[:8]:
        for cell in row:
            if UNIT_RE.search(cell):
                return cell
    for s in table.find_all_previous(string=True, limit=40):
        if UNIT_RE.search(clean(s)):
            return clean(s)
    return None


def unit_scale(table, rows: list[list[str]]) -> float | None:
    """Factor converting the amounts of a table to millions; None if the unit is not stated."""
    note = unit_note(table, rows)
    if note is None:
        return None
    return 0.001 if UNIT_RE.search(note).group(1).lower() == "thousands" else 1.0


def shares_scale(table, rows: list[list[str]], scale: float | None) -> float | None:
    """Factor converting the share counts of a table to millions: single shares if the table's unit excludes the number of shares (Sea)
    or names dollars only (Amphenol until 2015: "(dollars in millions, ...)"), else the unit of the amounts. A unit in the label of the share rows ("(in thousands):") overrides it, see parse_income."""
    note = unit_note(table, rows)
    return 1e-6 if note is not None and (RAW_SHARES_RE.search(note) or DOLLARS_ONLY_RE.search(note)) else scale


def currency_of(rows: list[list[str]]) -> str:
    """EUR if the unit line of the table says euros (ASML: "in millions EUR", "in millions €"), else USD."""
    return "EUR" if any(re.search(r"\bEUR\b|€", cell) for row in rows[:8] for cell in row) else "USD"


def header_cell(cell: str) -> str:
    """A header cell without its footnote mark or a leading "For the" / "As of": "2019*", "December 31, 2016 (1)" and "As of June 30,"
    become "2019", "December 31, 2016" and "June 30,"."""
    return PREFIX_RE.sub("", re.sub(r"\s*(?:\([\da-z]\)|\*+|†)+$", "", cell).strip())


def numbers(row: list[str]) -> list[float]:
    return [v for cell in row[1:] if (v := to_number(cell)) is not None]


def full_date(cell: str) -> pd.Timestamp | None:
    """"December 31, 2016" or "Dec 31, 2014"."""
    m = FULL_DATE_RE.match(header_cell(cell))
    if not m or m.group("month") not in MONTH_NUMBER:
        return None
    return pd.Timestamp(year=int(m.group("year")), month=MONTH_NUMBER[m.group("month")], day=int(m.group("day")))


def days_in(cells: list[str]) -> list[tuple[int, int]]:
    """The (month, day) of every cell like "September 30," or "Jul 4,"."""
    out = []
    for cell in cells:
        m = DAY_RE.match(header_cell(cell))
        if m and m.group("month") in MONTH_NUMBER:
            out.append((MONTH_NUMBER[m.group("month")], int(m.group("day"))))
    return out


def years_in(cells: list[str]) -> list[int]:
    """The years of a header row; "2017 3" is the year 2017 with the footnote number 3."""
    return [int(m.group(1)) for c in cells if (m := re.match(r"^((?:19|20)\d\d)(?:\s+\d)?$", header_cell(c)))]


def quarter_of(day: pd.Timestamp) -> pd.Period:
    """ASML's fiscal quarters end on the Sunday nearest to the calendar quarter end (July 3, 2022; January 2, 2022 for the fourth quarter of 2021)."""
    return pd.Period(day - pd.Timedelta(days=10), freq="Q")


class Columns(NamedTuple):
    first_row: int  # index of the first data row
    dates: list[pd.Timestamp]  # the period-end date of each value column of the group used
    offset: int  # how many value columns come before that group
    quarterly: bool  # True: the "three months" group; False: the only group is year-to-date (six or nine months, or the year)


def income_columns(rows: list[list[str]]) -> Columns | None:
    """The value columns of the three-months group of an income statement table; without one, of its only group if that is year-to-date
    (Sea's second- and third-quarter releases have six- and nine-months statements only).

    Forms of the header: groups with their date ("Three Months Ended September 30,"), groups and in the next row their dates, groups and in the next
    row one date per column ("Jul 4," "Jul 3," ...), or one full date per column ("Dec 31, 2014"); the years are in a row of their own or in the full dates.
    """
    for i, row in enumerate(rows[:10]):
        cells = [header_cell(c) for c in row]
        matches = [m for c in cells if (m := GROUP_RE.match(c))]
        kinds = [(m.group("kind") or "year").lower() for m in matches]
        group_days = [(MONTH_NUMBER[(m.group("month") or m.group("ymonth")).capitalize()], int(m.group("day") or m.group("yday"))) for m in matches]
        if not kinds:
            kinds = [(m.group("kind") or "year").lower() for c in cells if (m := KIND_RE.match(c))]
            group_days = []
        if not kinds or ("three" not in kinds and len(kinds) != 1):
            continue
        three = kinds.index("three") if "three" in kinds else 0
        quarterly = kinds[three] == "three"
        for j in range(i, min(i + 4, len(rows))):
            cells_j = [header_cell(c) for c in rows[j]]
            years = years_in(cells_j)
            dates = [d for c in cells_j if (d := full_date(c))]
            per_group = None
            if len(dates) >= 2 and len(dates) % len(kinds) == 0:  # one full date per column
                n = len(dates) // len(kinds)
                return Columns(j + 1, dates[three * n : (three + 1) * n], three * n, quarterly)
            if len(years) >= 2 and len(years) % len(kinds) == 0:
                n = len(years) // len(kinds)
                days = group_days or days_in([header_cell(c) for r in rows[i + 1 : j + 1] for c in r])  # the day of each group, or of each column
                if len(days) == len(kinds):
                    per_group = [days[three]] * n
                elif len(days) == len(years):
                    per_group = days[three * n : (three + 1) * n]
                if per_group is None:
                    continue
                return Columns(j + 1, [pd.Timestamp(year=y, month=month, day=day) for y, (month, day) in zip(years[three * n : (three + 1) * n], per_group)], three * n, quarterly)
    return None


def parse_income(rows: list[list[str]], scale: float | None, share_scale: float | None = None) -> tuple[pd.Period, dict[str, list[float]], bool] | None:
    """(period, values, quarterly): this period, the same period last year (and the previous quarter if the table has it) of each row."""
    header = income_columns(rows)
    if header is None or scale is None:
        return None
    first, columns, offset, quarterly = header
    note_column = any(c.lower() in ("note", "notes") for r in rows[:first] for c in r)  # a column of note numbers before the values (Sea 2017)
    found: dict[str, list[float]] = {}
    section = ""
    shares_unit = share_scale if share_scale is not None else scale
    for k, row in enumerate(rows[first:], first):
        label = NOTE_SUFFIX_RE.sub("", row[0])
        low = label.lower()
        nums = numbers(row)
        below = rows[k + 1] if k + 1 < len(rows) else []
        if nums and SHARES_SECTION_RE.match(low) and len(below) == 1 and BASIC_ROW_RE.match(below[0]):
            label = low = "basic"  # the numbers sit on the heading row, "Basic and diluted" on the next (Sea 2021-2022)
            section = "shares"
        if not nums:
            if SHARES_SECTION_RE.match(low):
                section = "shares"
                if m := SHARES_UNIT_RE.search(low):
                    shares_unit = 0.001 if m.group(1) == "thousands" else 1.0
            elif "per share" in low:
                section = "eps"
            continue
        if note_column and len(row) > 1 and re.fullmatch(r"\d{1,2}", row[1]):
            nums = nums[1:]
        if len(nums) < offset + len(columns):
            continue
        mine = nums[offset : offset + len(columns)]
        if REVENUE_RE.match(label):
            found.setdefault("revenue", mine)
        elif m := PROFIT_RE.match(label):
            found.setdefault("profit_owners" if m.group("owners") else "profit", mine)
        elif BASIC_EPS_RE.match(label) or (section == "eps" and BASIC_ROW_RE.match(low)):
            found.setdefault("eps", mine)
        elif BASIC_SHARES_RE.match(label) or (section == "shares" and BASIC_ROW_RE.match(low)):
            found.setdefault("wa_shares", [v * shares_unit for v in mine])
    owners = found.pop("profit_owners", None)
    if owners is not None and ("profit" not in found or any(re.search(r"non-?controlling", r[0], re.I) for r in rows[first:])):
        found["profit"] = owners  # with non-controlling interests (Sea): the row attributable to the company's shareholders, not the total net income
    if not {"revenue", "profit", "eps", "wa_shares"} <= found.keys():
        return None
    periods = [quarter_of(d) for d in columns]
    period = max(periods)
    order = [periods.index(q) for q in (period, period - 4, period - 1) if q in periods]  # this quarter, the same quarter last year, and the previous quarter if the table has it
    if len(order) < 2 or periods.index(period - 4) != order[1]:
        return None
    values = {}
    for key, nums in found.items():
        picked = [nums[c] for c in order]
        values[key] = [v * scale for v in picked] if key in ("revenue", "profit") else picked
    return period, values, quarterly


def parse_non_gaap(rows: list[list[str]]) -> tuple[pd.Period, list[float], bool] | None:
    """(period, [this period, same period last year], quarterly) of the basic Non-GAAP EPS row of a table with an income statement header
    (Dynatrace's reconciliation of GAAP to Non-GAAP net income); None if the table has no such row. Per-share data is as published."""
    header = income_columns(rows)
    if header is None:
        return None
    first, columns, offset, quarterly = header
    for row in rows[first:]:
        if not NON_GAAP_BASIC_EPS_RE.match(header_cell(NOTE_SUFFIX_RE.sub("", row[0]))):
            continue
        nums = numbers(row)
        if len(nums) < offset + len(columns):
            return None
        mine = nums[offset : offset + len(columns)]
        periods = [quarter_of(d) for d in columns]
        period = max(periods)
        if period - 4 not in periods:
            return None
        return period, [mine[periods.index(period)], mine[periods.index(period - 4)]], quarterly
    return None


def balance_dates(rows: list[list[str]]) -> list[pd.Timestamp] | None:
    """The date of each value column of a balance sheet: full dates in a header row, or the days in one row and the years in the next."""
    for r in rows[:8]:
        dates = [d for c in r if (d := full_date(c))]
        if len(dates) >= 2:
            return dates
    for r, below in zip(rows[:7], rows[1:8]):
        days, years = days_in([header_cell(c) for c in r]), years_in(below)
        if len(days) >= 2 and len(days) == len(years):
            return [pd.Timestamp(year=y, month=m, day=d) for (m, d), y in zip(days, years)]
        if len(days) == 1 and len(years) == 2 and len(r) == 1:  # one day over both years: "As of December 31," then "2021" "2022"
            return [pd.Timestamp(year=y, month=days[0][0], day=days[0][1]) for y in years]
    return None


def parse_balance(rows: list[list[str]], scale: float | None) -> tuple[pd.Period, list[float]] | None:
    """Equity at the end of this period and at the end of last year."""
    if scale is None:
        return None
    dates = balance_dates(rows)
    with_numbers = [r for r in rows if len(numbers(r)) >= 2]  # older releases: "Stockholders' Equity" (the heading has no numbers)
    row = next((r for r in with_numbers if OWNERS_EQUITY_RE.match(NOTE_SUFFIX_RE.sub("", r[0])) and not EQUITY_RE.match(NOTE_SUFFIX_RE.sub("", r[0]))), None)
    row = row or next((r for r in with_numbers if EQUITY_RE.match(NOTE_SUFFIX_RE.sub("", r[0]))), None)  # the company's own shareholders first, then the total
    if not dates or row is None:
        return None
    nums = numbers(row)[: len(dates)]
    if len(nums) != len(dates):
        return None
    order = sorted(range(len(dates)), key=lambda c: dates[c], reverse=True)  # the end of this period first (ASML lists the year end first)
    period = quarter_of(dates[order[0]])
    if quarter_of(dates[order[1]]) != pd.Period(year=period.year - 1, quarter=4, freq="Q"):
        # the comparison column is the end of a fiscal year that is not the calendar year (Dynatrace: March 31), while the history files the second
        # balance under the previous December quarter; only this period's balance is kept (the earlier one is in that period's own release), as for Alibaba
        return period, [nums[order[0]] * scale]
    return period, [nums[order[0]] * scale, nums[order[1]] * scale]


def close(a: float, b: float, key: str) -> bool:
    return abs(a - b) <= (0.005 if key == "eps" else max(1.0, abs(a) * TOLERANCE))


NUMBER = r"\(?-?[\d,]+(?:\.\d+)?\)?"


def text_numbers(match: re.Match, count: int) -> list[float]:
    return [to_number(match.group(k)) for k in range(1, count + 1)]


def parse_text(html: bytes) -> Parsed | None:
    """ASML's newest statements (2025 on) are positioned text without tables: the rows are found in the text of the page."""
    import html as htmllib

    text = clean(htmllib.unescape(re.sub(r"<[^>]+>", " ", html.decode("utf-8", "replace"))))
    start = text.find("Net system sales")  # the first statements: the later quarterly summary has five quarters
    if start < 0:
        return None
    block = text[max(0, start - 600) : start + 3500]
    header = re.search(r"((?:[A-Z][a-z]{2} \d{1,2}, ){2,4})\([Uu]naudited,[^)]*in millions[^)]*\) ((?:(?:19|20)\d\d ){2,4})", block)
    unit = re.search(r"in (millions|thousands)", block)
    if header is None or unit is None:
        return None
    days = [(MONTH_NUMBER[m.group(1)], int(m.group(2))) for m in re.finditer(r"([A-Z][a-z]{2}) (\d{1,2}),", header.group(1))]
    years = [int(y) for y in header.group(2).split()]
    if len(days) != len(years):
        return None
    groups = max(1, len(re.findall(r"(?:Three|Six|Nine|Twelve) months ended", block[max(0, header.start() - 120) : header.start()], re.I)))  # the first quarter has the three-months group only
    if len(days) % groups:
        return None
    n = len(days) // groups
    columns = [pd.Timestamp(year=y, month=m, day=d) for (m, d), y in zip(days[:n], years[:n])]  # the three-months group first
    scale = 0.001 if unit.group(1) == "thousands" else 1.0

    def row(label: str, count: int) -> list[float] | None:
        m = re.search(rf"{label} ({NUMBER}) ({NUMBER})(?: ({NUMBER}))?(?: ({NUMBER}))?", block[block.find("Net system sales") :])
        return None if m is None else [to_number(g) for g in m.groups()[:count] if g is not None]

    revenue, profit, eps = row("Total net sales", n * 2), row("Net income", n * 2), row("Basic net income per ordinary share", n * 2)
    shares_at = block.find("Basic net income per ordinary share")
    shares = re.search(rf"(?:in millions\) used in computing per share amounts|computing per share amounts \(in millions\)):? Basic ({NUMBER}) ({NUMBER})", block[shares_at:])
    if None in (revenue, profit, eps) or shares is None or len(revenue) < n:
        return None
    equity = re.search(rf"Total shareholders. equity ({NUMBER}) ({NUMBER})", text[text.find("Total shareholders"):] if "Total shareholders" in text else "")
    if equity is None:
        return None
    ordered = sorted(range(n), key=lambda c: columns[c], reverse=True)
    pick = lambda vals: [vals[c] for c in ordered[:2]]  # noqa: E731
    values = {
        "revenue": [v * scale for v in pick(revenue)],
        "profit": [v * scale for v in pick(profit)],
        "eps": pick(eps),
        "wa_shares": [v * scale for v in pick([to_number(shares.group(1)), to_number(shares.group(2))])],
        "equity": [to_number(equity.group(2)) * scale, to_number(equity.group(1)) * scale],  # the year end is listed first, then this quarter's end
    }
    return Parsed("EUR", values, quarter_of(columns[ordered[0]]))


# ---- Amphenol's Adjusted Diluted EPS (Non-GAAP), read only when the stock's pe_non_gaap is on diluted EPS (see parse_us_release) ------------------

ADJUSTED_ROW_RE = re.compile(r"^adjusted \(non-gaap\)", re.I)  # "Adjusted (non-GAAP) (1)", "Adjusted (non-GAAP) (ii) (iii)"
GROUP_HEADING_RE = re.compile(r"^(?P<kind>three|six|nine|twelve) months ended(?: (?P<month>[A-Z][a-z]+) \d{1,2})?[,:]?$", re.I)
GROUP_TEXT_RE = re.compile(r"\b(?P<kind>three|six|nine|twelve) months ended\b", re.I)
ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth"}
DOLLARS = r"\$(\d+\.\d\d)"


def visible(row: list[str]) -> list[str]:
    """The cells of a row without the empty ones (zero-width spaces) and lone dollar signs."""
    return [c for c in row if c.strip("\u200b $")]


def reconciliation_columns(rows: list[list[str]]) -> tuple[int, int, int] | None:
    """(columns per year group, position of "Diluted EPS" in a group, index of the header row) from the header labels of a reconciliation
    table: a row "Diluted" over a row "EPS" at the same positions, once in each of the two year groups (this year, last year)."""
    for i in range(1, min(len(rows), 10)):
        upper, lower = [header_cell(c) for c in visible(rows[i - 1])], [header_cell(c) for c in visible(rows[i])]
        eps = [k for k, c in enumerate(lower) if c == "EPS"]
        if len(lower) % 2 or len(eps) != 2 or len(upper) != len(lower):
            continue
        n = len(lower) // 2
        if eps[1] == eps[0] + n and all(upper[k] == "Diluted" for k in eps):
            return n, eps[0], i
    return None


def reconciliation_years(rows: list[list[str]], until: int) -> list[int]:
    """The years of the two groups: "2017" "2016", or "September 30, 2016" "September 30, 2015"."""
    for row in rows[:until]:
        cells = [header_cell(c) for c in visible(row)]
        years = years_in(cells) or [d.year for c in cells if (d := full_date(c))]
        if len(years) == 2:
            return years
    return []


def table_group(table, rows: list[list[str]]) -> tuple[str, str | None] | None:
    """The group of the first rows of a reconciliation table ("three", "September"), from a heading in the table or the text just before it."""
    for row in rows[:4]:
        cells = visible(row)
        if len(cells) == 1 and (m := GROUP_HEADING_RE.match(header_cell(cells[0]))):
            return m.group("kind").lower(), m.group("month")
    for text in table.find_all_previous(string=True, limit=30):
        if m := GROUP_TEXT_RE.search(clean(text)):
            month = re.search(r"months ended ([A-Z][a-z]+) \d{1,2}", clean(text), re.I)
            return m.group("kind").lower(), month.group(1) if month else None
    return None


def adjusted_eps_from_tables(soup, period: pd.Period) -> list[float] | None:
    """Adjusted Diluted EPS of this quarter and of the same quarter last year, from the three-months reconciliation table (Q3 2016 on).

    The columns are found by their header labels, not their positions (four per year group until 2017, five with the effective tax rate
    after). The fourth-quarter release of 2017 has one table with a block per quarter ("Three Months Ended December 31:" ...): the block of
    this quarter is taken.
    """
    month = pd.Timestamp(period.end_time).strftime("%B")
    found = []
    for table in soup.find_all("table"):
        rows = table_rows(table)
        if not any(ADJUSTED_ROW_RE.match(r[0]) for r in rows):
            continue
        columns = reconciliation_columns(rows)
        if columns is None:
            continue
        n, k, header = columns
        if reconciliation_years(rows, header) != [period.year, period.year - 1]:
            continue
        group = table_group(table, rows)
        for row in rows[header + 1 :]:
            cells = visible(row)
            if len(cells) == 1 and (m := GROUP_HEADING_RE.match(header_cell(cells[0]))):
                group = (m.group("kind").lower(), m.group("month"))
            if not ADJUSTED_ROW_RE.match(row[0]) or group is None or group[0] != "three" or group[1] not in (None, month):
                continue
            nums = numbers(visible(row))
            if len(nums) != 2 * n:
                raise ValueError(f"The reconciliation row {row[0]!r} has {len(nums)} numbers for {2 * n} columns")
            found.append([nums[k], nums[n + k]])
    if len({tuple(f) for f in found}) > 1:
        raise ValueError(f"The reconciliation tables in this release disagree on the Adjusted Diluted EPS: {found}")
    return found[0] if found else None


def adjusted_eps_from_text(soup, period: pd.Period) -> list[float] | None:
    """Adjusted Diluted EPS of this quarter from the first paragraph (the releases until Q2 2016, and Q4 2016, which has a twelve-months table only).

    Taken as stated: "fourth quarter 2014 diluted earnings per share excluding one-time items of $0.63", "first quarter 2016 diluted earnings
    per share of $0.59 (excluding one-time items)", "third quarter 2016 Adjusted Diluted EPS 1 was $0.73". Without such a figure the quarter had
    no one-time items and the release gives only its diluted EPS ("first quarter 2015 diluted earnings per share of $0.57 compared to $0.50
    (excluding one-time items) for the comparable 2014 period"): that figure is the adjusted one, provided the paragraph does not say the
    quarter included a charge. Anything else is not found (None): nothing is guessed.
    """
    text = clean(soup.get_text(" "))
    start = text.find("reported today")
    end = text.find("Sales for", start)
    if start < 0 or end < 0:
        return None
    lead = text[start:end]
    quarter = rf"{ORDINALS[period.quarter]} quarter(?: of)? {period.year}"
    stated = [
        rf"{quarter} diluted earnings per share excluding one-time items of {DOLLARS}",
        rf"{quarter} diluted earnings per share of {DOLLARS} \(excluding one-time items\)",
        rf"{quarter} adjusted diluted (?:earnings per share|EPS)(?: \d)? was {DOLLARS}",
    ]
    for pattern in stated:
        if m := re.search(pattern, lead, re.I):
            return [float(m.group(1))]
    included = rf"(?:for the {quarter}|{quarter}|for the {period.year} quarter) (?:includes|included)\b"
    plain = rf"(?:{quarter} (?:GAAP )?diluted earnings per share(?: \(EPS\))? of|(?:GAAP )?diluted earnings per share(?: \(EPS\))? for the {quarter} of) {DOLLARS}(?! \(excluding)(?!,? excluding)"
    if (m := re.search(plain, lead, re.I)) and not re.search(included, lead, re.I):
        return [float(m.group(1))]
    return None


def adjusted_diluted_eps(soup, period: pd.Period) -> list[float] | None:
    return adjusted_eps_from_tables(soup, period) or adjusted_eps_from_text(soup, period)


def parse_us_release(html: bytes, non_gaap: str | None = None) -> Parsed:
    """Parse a release. non_gaap is the EPS basis of the stock's Non-GAAP PE (None without one): "diluted" reads the Adjusted Diluted EPS
    (Amphenol) into non_ifrs_eps, "basic" the "Non-GAAP net income per share - basic" row of the reconciliation (Dynatrace, optional per release);
    stocks without a Non-GAAP PE get no Non-GAAP figure."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    income: list[tuple[pd.Period, dict[str, list[float]], bool, str]] = []
    balance: list[tuple[pd.Period, list[float]]] = []
    non_gaap_rows: list[tuple[pd.Period, list[float], bool]] = []  # basic Non-GAAP EPS rows (Dynatrace)
    for table in soup.find_all("table"):
        rows = table_rows(table)
        if not rows:
            continue
        scale = unit_scale(table, rows)
        if (found := parse_income(rows, scale, shares_scale(table, rows, scale))) is not None:
            income.append((*found, currency_of(rows)))
        if (found := parse_balance(rows, scale)) is not None:
            balance.append(found)
        if non_gaap == "basic" and (found := parse_non_gaap(rows)) is not None:
            non_gaap_rows.append(found)
    if not income:
        if not soup.find("table") or "Net system sales" in soup.get_text(" "):
            if (parsed := parse_text(html)) is not None:
                return parsed
        raise ValueError("Not found: the income statement (revenue, net income, basic EPS and basic shares for three months)")
    if not balance:
        raise ValueError("Not found: the balance sheet (total stockholders' equity)")
    if any(quarterly for _, _, quarterly, _ in income):
        income = [x for x in income if x[2]]  # the three-months statements; year-to-date ones only when there are none
    (period, values, quarterly, currency), *more = income
    for p, other, _, _ in more:
        if p == period and any(not close(a, b, key) for key, vals in other.items() for a, b in zip(vals, values[key])):
            raise ValueError(f"The income statements in this release disagree: {values} and {other}")
        if p == period and len(next(iter(other.values()))) > len(next(iter(values.values()))):
            values = other  # the table with the previous quarter as well (ASML's quarterly summary) has more to offer; the overlap was checked above
    equity_period, equity = balance[0]
    if equity_period != period:
        raise ValueError(f"The balance sheet is for {equity_period}, the income statement for {period}")
    values = {**values, "equity": equity}
    if non_gaap == "diluted" and (adjusted := adjusted_diluted_eps(soup, period)) is not None:
        values["non_ifrs_eps"] = adjusted
    elif non_gaap == "basic" and (found := [eps for p, eps, q in non_gaap_rows if p == period and q == quarterly]):  # Dynatrace's reconciliation row
        if any(not close(a, b, "eps") for other in found[1:] for a, b in zip(other, found[0])):
            raise ValueError(f"The basic Non-GAAP EPS in this release disagree: {found}")
        values["non_ifrs_eps"] = found[0]
    return Parsed(currency, values, period, quarterly)
