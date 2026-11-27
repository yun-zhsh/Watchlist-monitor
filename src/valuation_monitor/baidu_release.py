"""Extract earnings data from Baidu's quarterly earnings press releases (exhibit 99.1 of its Form 6-K on SEC EDGAR; verified on all releases
from the fourth quarter of 2014 on).

The releases are in RMB with a US dollar convenience translation, under US GAAP, but their tables are laid out differently from JD's and
Alibaba's (the `press_release` layout), so they have a layout of their own:

  header      the column dates are given in one of four ways: a row of full dates ("June 30, 2017"); a row of month and day ("June 30,") over
              a row of years, where the first cell of the year row may be the unit note ("(In RMB thousands except for share, per share
              (or ADS) information)", "2014", "2013"); or one cell per column with the group and the currency ("June 30, 2025 (RMB)",
              "Three months ended June 30, 2021 (RMB)"). A currency row (RMB, US$, "US$ (2)" with a footnote, USD) follows from 2018 on;
              before it every column is RMB. Group descriptions ("Three Months Ended", "Twelve Months Ended") come first.
  groups      a group ends after its US dollar column, or where a date repeats (the tables without a currency row); only the first group is
              read, and only when its description says three months; its RMB columns are told apart by date (this quarter, the same
              quarter last year, the previous quarter).
  income      revenue `Total revenues` (`Revenue` from 2025, `Revenues` in between); net income `Net income (loss) attributable to Baidu`
              (`... Baidu, Inc.` until 2015); basic EPS per ADS, the `Basic` row (`-Basic`, `— Basic`, `Net income attributable to Baidu,
              Inc.-Basic`) of the `Earnings per ADS` section; the basic EPS per share of the `Earnings per share` section (for the ratio of
              ADSs to shares); the basic weighted average ordinary shares.
  ADS ratio   the basic EPS per ADS / the basic EPS per share of the same release, a whole number or its inverse: 1/10 until the 2021
              subdivision (ten ADSs per Class A share), 8 since (one ADS for eight Class A shares). The per-ADS figures are on the same basis
              before and after (one ADS stayed one ADS), only the share counts change; ADSs = shares / ratio.
  shares      millions when the section says "(in millions)"; single shares when the unit note says "except for share" or "except for
              number of shares" (until 2020); otherwise in the unit of the table.
  Non-GAAP    Baidu publishes only a diluted Non-GAAP EPS per ADS, used as stated (never derived): `Non-GAAP diluted earnings per ADS`
              (until 2018) or `Diluted earnings per ADS (non-GAAP)`, in the reconciliation table; from 2019 that table has one column per
              segment (Baidu Core, iQIYI, Baidu, Inc.) but its EPS row only one value per date, which is how it is read. The 2014-2015
              releases have no Non-GAAP EPS.
  balance     `Total Baidu shareholders' equity` (`Total Baidu, Inc. shareholders' equity`), at the end of this quarter and of last year.

Units (thousands or millions) are in the unit note of the table, or in the line before it, and are converted to millions. A row is only read
when it has one number per column. When the same value appears in several tables it must agree, otherwise an error is raised.
"""

from __future__ import annotations

import re
from typing import NamedTuple

import pandas as pd

from valuation_monitor.parsed import Parsed
from valuation_monitor.press_release import MONTHS, clean, table_rows, to_number

MONTH_DAY_RE = re.compile(r"^([A-Z][a-z]+)\.? ?(\d{1,2}),?$")
SHORT_MONTHS = {m[:3]: i for m, i in MONTHS.items()} | {"Sept": 9}  # "Mar 31," in a few balance sheets
YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
# "June 30, 2025 (RMB)", "Three months ended June 30, 2021 (US$)", "Twelve months ended December 31, 2013"
DATE_CELL_RE = re.compile(
    r"^(?:(?P<group>(?:three|six|nine|twelve) months ended) )?(?P<month>[A-Z][a-z]+)\.? ?(?P<day>\d{1,2}),? (?P<year>\d{4})(?: \((?P<ccy>RMB|US\$|USD)\))?$", re.I
)
CURRENCY_RE = re.compile(r"^(RMB|US\$|USD)(?: \(\d\))?$")
GROUP_RE = re.compile(r"\b(?:three|six|nine|twelve)[- ]months? ended\b|^for the (?:three|six|nine|twelve)-month ended$", re.I)
THREE_MONTHS_RE = re.compile(r"\bthree[- ]months?\b", re.I)
UNIT_RE = re.compile(r"\bin (?:RMB )?(thousands|millions)\b", re.I)
SINGLE_SHARES_RE = re.compile(r"except for (?:share|number of shares)\b", re.I)
HEADER_FILLER_RE = re.compile(r"^(?:un)?audited$|^\(.*\)$", re.I)  # "Unaudited", "Audited", the unit note

REVENUE_RE = re.compile(r"^(?:total revenues|revenues?)(?: ?\(note \d\))?$", re.I)
PROFIT_RE = re.compile(r"^net (?:income(?: \(loss\))?|\(loss\) income|loss) attributable ?to baidu(?:, inc\.)?$", re.I)  # "attributableto" in the 2023 releases
EQUITY_RE = re.compile(r"^total baidu(?:, inc\.)? shareholders.? equity$", re.I)
EPS_ADS_SECTION_RE = re.compile(r"^(?:earnings|\(loss\) earnings|earnings \(loss\)|loss) per ads\b", re.I)
EPS_SHARE_SECTION_RE = re.compile(r"^(?:earnings|\(loss\) earnings|earnings \(loss\)|loss) per share\b", re.I)
SHARES_SECTION_RE = re.compile(r"^weighted average number of class a and class b ordinary shares", re.I)
BASIC_RE = re.compile(r"^(?:net income(?: \(loss\))? attributable to baidu(?:, inc\.)? ?)?[-—–]? ?basic$", re.I)
NON_GAAP_EPS_RE = re.compile(r"^(?:non-gaap diluted earnings(?: \(loss\))? per ads|diluted earnings(?: \(loss\))? per ads \(non-gaap\))(?: ?\d)?$", re.I)

# The definition of the Non-GAAP net income, in the notes of every release with a Non-GAAP EPS: "Non-GAAP net income attributable to Baidu
# represents net income attributable to Baidu excluding share-based compensation expenses, ..." (up to the end of the sentence)
DEFINITION_RE = re.compile(r"Non-GAAP net income attributable to Baidu represents net income attributable to Baidu excluding ([^.]*)\.", re.I)
FAIR_VALUE_RE = re.compile(r"\bfair value\b", re.I)  # excluded from the first quarter of 2018
AMORTIZATION_RE = re.compile(r"\bamortization (?:and impairment )?of intangible assets resulting from business combinations\b", re.I)  # from the first quarter of 2019
DEFINITION_2016_NOTE = (
    "Baidu's Non-GAAP definition until the fourth quarter of 2017: it excluded only share-based compensation and the gain or loss on shares "
    "issued by equity method investees, so the disposal gains and losses, impairments and fair value changes of long-term investments were "
    "still included (excluded from the first quarter of 2018 on), and so were the amortization of intangible assets resulting from business "
    "combinations and Baidu's share of the Non-GAAP reconciling items of its equity method investees (excluded from the first quarter of 2019 on)"
)  # no "; " in a note: the saved note column joins the notes with it
DEFINITION_2018_NOTE = (
    "Baidu's Non-GAAP definition of 2018: it excluded share-based compensation and the disposal gains and losses, impairments and fair value "
    "changes of long-term investments, but not yet the amortization of intangible assets resulting from business combinations or Baidu's "
    "share of the Non-GAAP reconciling items of its equity method investees (both excluded from the first quarter of 2019 on)"
)
RATIO_TOLERANCE = 0.05  # allowed relative distance of (EPS per ADS / EPS per share) from a whole number or its inverse
TOLERANCE = 0.001  # allowed (relative) difference of the same amount in different tables
PER_SHARE_KEYS = ("eps", "eps_share", "non_ifrs_eps")


class NoStatements(ValueError):
    """A results release without its financial statements (the fourth-quarter 2020 release on EDGAR has the summary tables only)."""


class Header(NamedTuple):
    dates: list[pd.Timestamp]  # one per column
    currencies: list[str]  # "RMB" or "USD", one per column
    labels: list[str]  # group descriptions
    first_data_row: int
    unit_note: str  # the unit note found in the header ("" if none)
    audited: list[str] | None = None  # "Unaudited" or "Audited", one per column, when the header has such a row


def _date_cells(row: list[str]) -> tuple[list[pd.Timestamp], list[str | None], list[str]] | None:
    dates, ccys, groups = [], [], []
    for cell in row:
        m = DATE_CELL_RE.match(cell)
        month = m and (MONTHS.get(m.group("month")) or SHORT_MONTHS.get(m.group("month")))
        if not month:
            return None
        dates.append(pd.Timestamp(year=int(m.group("year")), month=month, day=int(m.group("day"))))
        ccys.append({"RMB": "RMB", None: None}.get(m.group("ccy"), "USD") if m.group("ccy") else None)
        if m.group("group"):
            groups.append(m.group("group"))
    return dates, ccys, groups


def _month_day(cell: str) -> tuple[int, int] | None:
    m = MONTH_DAY_RE.match(cell)
    month = m and (MONTHS.get(m.group(1)) or SHORT_MONTHS.get(m.group(1)))
    return (month, int(m.group(2))) if month else None


def parse_header(rows: list[list[str]]) -> Header | None:
    """The column dates, currencies and group descriptions at the top of a table; None if the table has no such header."""
    labels: list[str] = []
    dates: list[pd.Timestamp] | None = None
    ccys: list[str | None] | None = None
    month_days: list[tuple[int, int]] | None = None
    unit_note = ""
    audited = None
    i = 0
    for i, row in enumerate(rows[:8]):
        cells = list(row)
        if cells and HEADER_FILLER_RE.match(cells[0]) and len(cells) > 1 and cells[0].startswith("("):
            unit_note = cells[0]  # the unit note in the first cell of the date or currency row
            cells = cells[1:]
        if dates is None and (found := _date_cells(cells)) is not None:
            dates, ccys, groups = found
            labels += groups
            continue
        if all(GROUP_RE.search(c) for c in cells):
            labels += cells
            continue
        if month_days is None and dates is None and all(_month_day(c) for c in cells):
            month_days = [_month_day(c) for c in cells]
            continue
        if month_days is not None and dates is None and all(YEAR_RE.match(c) for c in cells):
            if len(cells) != len(month_days):
                return None
            dates = [pd.Timestamp(year=int(y), month=m, day=d) for y, (m, d) in zip(cells, month_days)]
            ccys = [None] * len(dates)
            continue
        if dates is not None and all(CURRENCY_RE.match(c) for c in cells):
            if len(cells) != len(dates):
                return None
            ccys = ["RMB" if c == "RMB" else "USD" for c in cells]
            continue
        if dates is not None and audited is None and len(cells) == len(dates) and all(re.match(r"^(?:un)?audited$", c, re.I) for c in cells):
            audited = [c.lower() for c in cells]
            continue
        if all(HEADER_FILLER_RE.match(c) for c in cells) or (len(cells) == 1 and not to_number(cells[0])):
            if len(cells) == 1 and UNIT_RE.search(cells[0]):
                unit_note = cells[0]
            if dates is None:
                continue
        break
    else:
        i += 1
    if dates is None:
        return None
    return Header(dates, [c or "RMB" for c in ccys], labels, i, unit_note, audited)


def three_month_columns(header: Header) -> list[int] | None:
    """The RMB columns of the first group, when it is the three months; None otherwise.

    A group ends after its US dollar column, or before a date that is already in it (tables without a currency row)."""
    if not header.labels or not THREE_MONTHS_RE.search(header.labels[0]):
        return None
    seen: set[pd.Timestamp] = set()
    out = []
    for j, (d, ccy) in enumerate(zip(header.dates, header.currencies)):
        if ccy != "RMB":
            break
        if d in seen:
            break
        seen.add(d)
        out.append(j)
    return out


def unit_scale(table, header: Header) -> float | None:
    """Factor converting amounts to millions, from the unit note of the table or the text before it; None if there is none."""
    for note in [header.unit_note] if header.unit_note else outside_notes(table):
        if m := UNIT_RE.search(note):
            return 0.001 if m.group(1).lower() == "thousands" else 1.0
    return None


def outside_notes(table) -> list[str]:
    """The text just before a table, outside any other table (a label inside an earlier table, "(in millions)" of a share count, is not a unit note)."""
    return [clean(s) for s in table.find_all_previous(string=True, limit=40) if s.find_parent("table") is None]


def unit_note_text(table, header: Header) -> str:
    if header.unit_note:
        return header.unit_note
    return next((note for note in outside_notes(table) if UNIT_RE.search(note)), "")


def row_values(header: Header, row: list[str]) -> list[float] | None:
    """The numbers of a data row, one per column; None if the row does not have exactly one number per column."""
    nums = [to_number(c) for c in row[1:]]
    if len(nums) != len(header.dates) or any(v is None for v in nums):
        return None
    return nums


def quarter_values(header: Header, columns: list[int], values: list[float]) -> tuple[pd.Period, list[float]]:
    """[this quarter, same quarter last year, (previous quarter)] from the three-month columns."""
    periods = {pd.Period(header.dates[j], freq="Q"): values[j] for j in columns}
    current = max(periods)
    out = [periods.get(current), periods.get(current - 4), periods.get(current - 1)]
    if out[0] is None or out[1] is None:
        raise ValueError(f"The quarterly data lacks the current or the prior-year quarter: {sorted(periods)}")
    return current, out[:2] if out[2] is None else out


def parse_flows(rows: list[list[str]], header: Header, scale: float | None, single_shares: bool) -> dict[str, tuple[pd.Period, list[float]]]:
    columns = three_month_columns(header)
    if not columns:
        return {}
    found: dict[str, tuple[pd.Period, list[float]]] = {}
    section = None
    for row in rows[header.first_data_row :]:
        label = row[0]
        values = row_values(header, row)
        if len(row) == 1:
            if EPS_ADS_SECTION_RE.match(label):
                section = "eps"
            elif EPS_SHARE_SECTION_RE.match(label):
                section = "eps_share"
            elif SHARES_SECTION_RE.match(label):
                section = "wa_shares_millions" if re.search(r"\(in millions\)", label, re.I) else "wa_shares"
            continue
        if values is None:
            continue
        key = None
        if BASIC_RE.match(label) and section:
            key = "wa_shares" if section.startswith("wa_shares") else section
        elif NON_GAAP_EPS_RE.match(label):
            key = "non_ifrs_eps"
        elif REVENUE_RE.match(label):
            key = "revenue"
        elif PROFIT_RE.match(label):
            key = "profit"
        if key is None or key in found:
            continue
        period, vals = quarter_values(header, columns, values)
        if key in ("revenue", "profit"):
            if scale is None:
                continue
            vals = [v * scale for v in vals]
        elif key == "wa_shares":
            if section == "wa_shares_millions":
                factor = 1.0
            elif single_shares:
                factor = 1e-6
            elif scale is not None:
                factor = scale
            else:
                continue
            vals = [v * factor for v in vals]
        found[key] = (period, vals)
        if key == "wa_shares":
            section = None
    return found


def parse_balance(rows: list[list[str]], header: Header, scale: float | None) -> tuple[pd.Period, list[float]] | None:
    if scale is None:
        return None
    for row in rows[header.first_data_row :]:
        if EQUITY_RE.match(row[0]) and (values := row_values(header, row)) is not None:
            rmb = [j for j in range(len(values)) if header.currencies[j] == "RMB"]
            dates = [header.dates[j] for j in rmb]
            if len(set(dates)) != len(dates):
                # the fourth-quarter 2015 release dates both columns "December 31 2015": the unaudited one is this period's end, the audited one last year's
                if len(rmb) != 2 or header.audited is None or sorted(header.audited[j] for j in rmb) != ["audited", "unaudited"]:
                    raise ValueError(f"The balance sheet has the same date twice: {dates}")
                dates = [d if header.audited[j] == "unaudited" else pd.Timestamp(year=d.year - 1, month=12, day=31) for d, j in zip(dates, rmb)]
            by_date = {d: values[j] for d, j in zip(dates, rmb)}
            current = max(by_date)
            earlier = pd.Timestamp(year=current.year - 1, month=12, day=31)
            if earlier not in by_date:
                raise ValueError(f"The balance sheet lacks the end of last year: {sorted(by_date)}")
            return pd.Period(current, freq="Q"), [by_date[current] * scale, by_date[earlier] * scale]
    return None


def ads_ratio(eps_ads: list[float], eps_share: list[float]) -> float:
    """Ordinary shares per ADS: the basic EPS per ADS / per share of the same release, on the largest figure (least rounding);
    a whole number (8 since 2021) or the inverse of one (1/10 before: ten ADSs per share)."""
    k = max(range(len(eps_share)), key=lambda i: abs(eps_share[i]))
    if eps_share[k] == 0 or eps_ads[k] == 0:
        raise ValueError("Cannot derive the shares per ADS: the basic EPS is zero")
    ratio = eps_ads[k] / eps_share[k]
    for value in (ratio, 1 / ratio):
        whole = round(value)
        if whole >= 1 and abs(value - whole) <= RATIO_TOLERANCE * whole:
            return float(whole) if value == ratio else 1 / whole
    raise ValueError(f"The basic EPS per ADS / per share is not a whole number of shares or ADSs: {eps_ads} / {eps_share}")


def close(a: float, b: float, key: str) -> bool:
    return abs(a - b) <= (0.005 if key in PER_SHARE_KEYS else max(1.0, abs(a) * TOLERANCE))


def parse_baidu_release(html: bytes) -> Parsed:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    candidates: dict[str, list[tuple[pd.Period, list[float]]]] = {}
    previous: tuple[Header, float | None] | None = None
    for table in soup.find_all("table"):
        rows = table_rows(table)
        header = parse_header(rows)
        if header is None:
            continue
        scale = unit_scale(table, header)
        if scale is None and previous and previous[0].dates == header.dates:
            scale = previous[1]  # a statement continued in a table of its own after a page break (the liabilities and equity of a balance sheet)
        previous = header, scale
        single_shares = bool(SINGLE_SHARES_RE.search(unit_note_text(table, header)))
        for key, found in parse_flows(rows, header, scale, single_shares).items():
            candidates.setdefault(key, []).append(found)
        if (equity := parse_balance(rows, header, scale)) is not None:
            candidates.setdefault("equity", []).append(equity)
    statements = {"revenue", "profit", "eps", "eps_share", "wa_shares", "equity"}
    if not statements & candidates.keys():
        raise NoStatements("The release has no income statement and no balance sheet")
    missing = statements - candidates.keys()
    if missing:
        raise ValueError(f"Not found: {sorted(missing)}")
    values: dict[str, list[float]] = {}
    periods = set()
    for key, found in candidates.items():
        (period, first), *others = found
        for p, other in others:
            if p != period or len(other) != len(first) or any(not close(a, b, key) for a, b in zip(other, first)):
                raise ValueError(f"{key} differs between tables: {found}")
        values[key] = first
        periods.add(period)
    if len(periods) != 1:
        raise ValueError(f"The rows disagree on the reporting period: {periods}")
    ratio = ads_ratio(values["eps"], values.pop("eps_share"))
    values["wa_shares"] = [v / ratio for v in values["wa_shares"]]  # millions of shares -> millions of ADSs
    notes = {}
    if "non_ifrs_eps" in values and (note := definition_note(soup.get_text(" "))):
        notes["non_ifrs_eps"] = note
    return Parsed("CNY", values, periods.pop(), notes=notes or None)


def definition_note(text: str) -> str | None:
    """The note on a Non-GAAP EPS of one of the older definitions, told apart by the definition the release states; None for the current one."""
    m = DEFINITION_RE.search(clean(text))
    if m is None:
        raise ValueError("Not found: the definition of the Non-GAAP net income")
    if not FAIR_VALUE_RE.search(m.group(1)):
        return DEFINITION_2016_NOTE
    if not AMORTIZATION_RE.search(m.group(1)):
        return DEFINITION_2018_NOTE
    return None


# ---- the fourth quarter of 2020: the statements of the release on Baidu's IR website, supplied by the maintainer ------------------------
#
# The 6-K of 2021-02-19 (accession 0001193125-21-049165) furnishes only the first ten pages of the release (the summary tables and the text);
# the full release on Baidu's IR website has the statements on pages 11 to 18. Baidu's IR website refuses requests from cloud networks, so the
# maintainer supplied a copy of that PDF; its first page and the statement pages are kept, unchanged, in the package (supplied/BIDU/). The
# figures are read from its text, with the publication time of the 6-K (the filing whose exhibit lacks them).

SUPPLIED_DIR = "supplied"
SUPPLIED_STATEMENTS = {  # the SEC exhibit without statements -> the supplied PDF with them
    "https://www.sec.gov/Archives/edgar/data/1329099/000119312521049165/d102906dex991.htm": "BIDU/2021-02-19_baidu-2020q4-ir-release-statements.pdf",
}
SUPPLIED_NOTE = (
    "from the statements of Baidu's release on its IR website (pages 11-18 of the PDF, supplied by the maintainer, kept in "
    "src/valuation_monitor/supplied/{file}), because the 6-K exhibit furnished to the SEC omits them, and used from the 6-K's publication time"
)  # no "; " in a note: the saved note column joins the notes with it
PDF_NUMBER_RE = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")
# the column headers of the supplied statements, which fix the order of their columns
SUPPLIED_INCOME_HEADER = (
    "December 31, September 30, December 31, December 31, December 31, December 31, December 31,",
    "2019 2020 2020 2020 2019 2020 2020",
    "RMB RMB RMB US$(2) RMB RMB US$(2)",
)  # this quarter is the third column, the same quarter last year the first, the previous quarter the second (then the US$ and the years)
SUPPLIED_BALANCE_HEADER = ("December 31, December 31, December 31,", "2019 2020 2020", "RMB RMB US$")  # end of last year, end of this year, US$
SUPPLIED_RECONCILIATION_HEADER = "December 31, 2019 (RMB) September 30, 2020 (RMB) December 31, 2020 (RMB) December 31, 2020 (US$)"


def supplied_file(url: str) -> str | None:
    """The supplied PDF with the statements of the release at this URL, None if there is none."""
    return SUPPLIED_STATEMENTS.get(url)


def _pdf_row(lines: list[str], label: str, start: int = 0) -> tuple[list[float], int]:
    """The numbers of the first line from `start` that is `label` followed by numbers only, and the index of that line."""
    for i in range(start, len(lines)):
        line = clean(lines[i])
        if line.startswith(label + " "):
            cells = line[len(label):].split()
            if cells and all(PDF_NUMBER_RE.match(c) for c in cells):
                return [to_number(c) for c in cells], i
    raise ValueError(f"Not found in the supplied statements: {label}")


def _page_with(pages: list[str], heading: str, *headers: str) -> list[str]:
    for text in pages:
        lines = [clean(x) for x in text.splitlines()]
        if any(heading in x for x in lines) and all(h in lines for h in headers):
            return lines
    raise ValueError(f"The supplied statements lack the page {heading!r} with the header {headers}")


def parse_supplied_statements(pdf: bytes, html: bytes, file: str) -> Parsed:
    """The fourth quarter of 2020 from the supplied statement pages (see SUPPLIED_STATEMENTS); html is the SEC exhibit, for its definition."""
    import io

    from pypdf import PdfReader

    pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages]
    if "Baidu Announces Fourth Quarter and Fiscal Year 2020 Results" not in clean(pages[0]):
        raise ValueError("The supplied PDF is not the release of the fourth quarter of 2020")
    income = _page_with(pages, "Condensed Consolidated Statements of Income (Loss)", *SUPPLIED_INCOME_HEADER)
    order = [2, 0, 1]  # this quarter, the same quarter last year, the previous quarter
    values: dict[str, list[float]] = {}
    for key, label in (("revenue", "Total revenues"), ("profit", "Net income attributable to Baidu")):
        row, _ = _pdf_row(income, label)
        values[key] = [row[j] for j in order]
    ads_at = next(i for i, x in enumerate(income) if x.startswith("Earnings per ADS"))
    eps, eps_at = _pdf_row(income, "-Basic", ads_at)
    share_at = next(i for i, x in enumerate(income) if x.startswith("Earnings per share"))
    eps_share, _ = _pdf_row(income, "-Basic", share_at)
    if not ads_at < eps_at < share_at:
        raise ValueError("The supplied statements: the basic EPS per ADS is not in its section")
    shares_at = next(i for i, x in enumerate(income) if x.startswith("Weighted average number of Class A and Class B ordinary shares outstanding"))
    shares, _ = _pdf_row(income, "Basic", shares_at)
    values["eps"] = [eps[j] for j in order]
    ratio = ads_ratio(values["eps"], [eps_share[j] for j in order])
    values["wa_shares"] = [shares[j] * 1e-6 / ratio for j in order]  # single shares ("except for share ... information") -> millions of ADSs
    balance = _page_with(pages, "Condensed Consolidated Balance Sheets", *SUPPLIED_BALANCE_HEADER)
    equity, _ = _pdf_row(balance, "Total Baidu shareholders' equity")
    values["equity"] = [equity[1], equity[0]]  # the end of this year, the end of last year
    reconciliation = _page_with(pages, "Reconciliations of Non-GAAP Financial Measures", SUPPLIED_RECONCILIATION_HEADER)
    non_gaap, _ = _pdf_row(reconciliation, "Diluted earnings per ADS (non-GAAP)")
    if len(non_gaap) != 4:
        raise ValueError(f"The supplied Non-GAAP EPS row has {len(non_gaap)} values, not one per date")
    values["non_ifrs_eps"] = [non_gaap[j] for j in order]
    from bs4 import BeautifulSoup

    note = SUPPLIED_NOTE.format(file=file)
    notes = {key: note for key in values}
    if definition := definition_note(BeautifulSoup(html, "lxml").get_text(" ")):
        notes["non_ifrs_eps"] = f"{note}. {definition}"
    return Parsed("CNY", values, pd.Period("2020Q4"), notes=notes)


def parse_baidu_filing(url: str, html: bytes) -> Parsed:
    """A Baidu release; one without statements is read from the statements supplied for it (SUPPLIED_STATEMENTS), else it is an error."""
    try:
        return parse_baidu_release(html)
    except NoStatements:
        file = supplied_file(url)
        if file is None:
            raise
        from importlib.resources import files

        pdf = files("valuation_monitor").joinpath(SUPPLIED_DIR, *file.split("/")).read_bytes()
        return parse_supplied_statements(pdf, html, file)
