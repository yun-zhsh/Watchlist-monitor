"""Extract earnings data from Tesla's quarterly "Update" (exhibit 99.1 of its 8-K with item 2.02; the shareholder letter until 2015).

The Update is a slide deck. Its statements are not HTML tables (except for 2017 to the second quarter of 2019) but text, so the
document is read as one line of text (tags removed, whitespace collapsed) and the rows are found in it:

  income statement   the statement of operations: a header with the period-end date of each column, then rows with one figure per column.
                     Headers: full dates ("Three Months Ended Six Months Ended June 30, 2015 Mar 31, 2015 June 30, 2014 June 30, 2015
                     June 30, 2014"; "31-Dec-18 30-Sep-19 31-Dec-19 31-Dec-18 31-Dec-19"), days over years ("Sept 30, June 30, Sept 30, Sept 30,
                     Sept 30, 2016 2016 2015 2016 2015") or quarters ("Q2-2025 Q3-2025 Q4-2025 Q1-2026 Q2-2026", five quarters in order, the
                     third quarter of 2019 on except for that quarter and the next). With a year-to-date group (six, nine months or the year)
                     the first three columns are the three-months group. The figures taken: "Total revenues", the net income attributable to
                     common stockholders (the net income or loss before Tesla had non-controlling interests, 2015 and 2016), the basic EPS
                     ("Net loss per common share, basic and diluted" in a loss quarter) and the basic weighted average shares.
  balance sheet      "Total stockholders' equity" ("Stockholders' equity" until 2016) at the end of this quarter and of last year: the
                     non-controlling interests are listed after it, so it is Tesla's own.
  Non-GAAP EPS       only for a stock with a Non-GAAP PE on diluted EPS: the diluted Non-GAAP EPS of the reconciliation of GAAP to Non-GAAP
                     ("Net income (loss) per share, diluted (Non-GAAP)", "... diluted - non-GAAP", "EPS attributable to common stockholders,
                     diluted (non-GAAP)"; "basic and diluted - Non-GAAP" in the loss quarters of 2017), with the columns of its own header.
                     Until the second quarter of 2016 the definition also excluded the non-cash interest expense on convertible notes and added
                     back the gross profit deferred under lease accounting: such a figure carries a note (Parsed.notes), see OLD_DEFINITION_NOTE.

Amounts are converted to millions ("In thousands" until the second quarter of 2019, also for the shares), per-share data is as published.
A deck whose statements are page images only (the fourth quarter of 2016, and the fourth quarter of 2020 to the first quarter of 2023) has no
figures in its text: statements_in_text tells it apart, and its quarter is read from the Form 10-Q or 10-K instead (see providers/sec.py).
A figure that is not found is an error: nothing is guessed.
"""

from __future__ import annotations

import html as htmllib
import re

import pandas as pd

from valuation_monitor.parsed import Parsed

MONTHS = {m: k for k, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
# one column of a header: "June 30, 2015", "Sept 30 2016", "31-Dec-18", "Q3-2025"
DATE_RE = re.compile(rf"(?P<month>{MONTH}) (?P<day>\d{{1,2}}),? (?P<year>(?:19|20)\d\d)\b|\b(?P<dday>\d{{1,2}})-(?P<dmonth>{MONTH})-(?P<dyear>\d\d)\b|\bQ(?P<q>[1-4])-(?P<qyear>20\d\d)\b")
DAY_RE = re.compile(rf"(?P<month>{MONTH}) (?P<day>\d{{1,2}}),?")
YEAR_RE = re.compile(r"\b(?:19|20)\d\d\b")
GROUP_RE = re.compile(r"\b(?:three|six|nine|twelve) months end(?:ed|ing)\b|\byears? end(?:ed|ing)\b", re.I)
VALUE_RE = re.compile(r"\s*(?:\$\s*)?(\(\s*\d[\d,]*(?:\.\d+)?\s*\)|-?\d[\d,]*(?:\.\d+)?|[—–-])(?=\s|$)")
FOOTNOTE_RE = re.compile(r"\s*\(\d[A-Z]?\)")  # "(1)", "(1A)" after a label
GAP = r"(?:[^$\d]|\(\d[A-Z]?\))"  # label text: no figures, footnote marks allowed

STATEMENT_START_RE = re.compile(r"Statements? of Operations|F I N A N C I A L S T A T E M E N T S", re.I)
STATEMENT_END_RE = re.compile(r"Statements? of Comprehensive (?:Income|Loss)|Balance Sheets?|B A L A N C E S H E E T", re.I)
BALANCE_START_RE = re.compile(r"Balance Sheets?|B A L A N C E S H E E T", re.I)
REVENUE_RE = re.compile(r"\bTotal revenues\b")
PROFIT_OWNERS_RE = re.compile(r"\bNet (?:\((?:loss|income)\) )?(?:income|loss)(?: \((?:loss|income)\))? attributable to common stockholders\b(?! \((?:non-)?GAAP\))", re.I)
PROFIT_RE = re.compile(r"\bNet (?:\((?:loss|income)\) )?(?:income|loss)(?: \((?:loss|income)\))?(?= \$| \(| \d)", re.I)
# "... per share of common stock attributable to common stockholders – basic and diluted Basic $ 1.82" (2018 and 2019) as well
# the label starts with the net income or loss, not with "Weighted average shares used in computing net income per share ..." (the shares row)
EPS_RE = re.compile(
    rf"(?<!computing )\bnet (?:\((?:loss|income)\) )?(?:income|loss)(?: \((?:loss|income)\))? per (?:common )?share\b{GAP}{{0,100}}?\bbasic(?: and diluted)?(?: Basic)?\b", re.I
)
SHARES_RE = re.compile(rf"\b(?:Shares used in per share calculation|Weighted average shares used in computing){GAP}{{0,100}}?\bbasic(?: and diluted)?(?: Basic)?\b", re.I)
EQUITY_RE = re.compile(r"\b(?:Total )?stockholders.? equity(?= \$| \(| \d)", re.I)
NON_GAAP_DILUTED_RE = re.compile(
    rf"(?:\bper share(?: attributable to common stockholders)?|\bEPS attributable to common stockholders),? (?:basic and )?diluted\s*(?:[-–]\s*)?\(?non-GAAP\)?", re.I
)
NON_GAAP_BASIC_RE = re.compile(r"\b(?:per share(?: attributable to common stockholders)?|EPS attributable to common stockholders),? basic\s*\(non-GAAP\)", re.I)
UNIT_RE = re.compile(r"\bIn (thousands|millions)\b", re.I)
# the reconciliation of the definition used until the second quarter of 2016 (see the module docstring)
OLD_DEFINITION_RE = re.compile(r"Non-cash interest expense related to convertible notes|gross profit deferred due to lease accounting", re.I)
OLD_DEFINITION_NOTE = (
    "Tesla's definition until the second quarter of 2016: besides stock-based compensation it also excluded the non-cash interest expense "
    "related to convertible notes and other borrowing and added back the gross profit deferred due to lease accounting (cars sold with a "
    "resale value guarantee), while from the third quarter of 2016 Tesla's Non-GAAP EPS excludes mainly stock-based compensation"
)  # no "; " in a note: the saved note column joins the notes with it
# the title of an Update deck (see providers/sec.py): "Q3 2026 Update", "Q4 and FY2019 Update", "Q4 and FY 2023 Update", "Tesla Third Quarter 2016 Update",
# "Tesla Fourth Quarter & Full Year 2015 Update", "Tesla Motors – First Quarter 2015 Shareholder Letter"
TITLE_RE = re.compile(
    r"\bTesla(?: Motors)?(?: [–-])? (?P<ordinal>First|Second|Third|Fourth) Quarter(?: & Full Year)? (?P<year>20\d\d) (?:Update|Shareholder Letter)\b"
    r"|\bQ(?P<q>[1-4])(?: and FY ?| )(?P<qyear>20\d\d) Update\b"
)
ORDINALS = {"First": 1, "Second": 2, "Third": 3, "Fourth": 4}
HEADER_SPAN = 8000  # how far before a row its header may be
MAX_COLUMNS = 5
STATEMENT_SPAN = 4000  # how far after the heading of the statement of operations its revenue row may be
RECONCILIATION_SPAN = 3000  # how far before the diluted Non-GAAP EPS row the reconciliation's items are


def document_text(html: bytes) -> str:
    """The text of the deck in one line: tags removed, entities decoded, whitespace collapsed."""
    text = re.sub(r"<[^>]+>", " ", html.decode("utf-8", "replace"))
    return " ".join(htmllib.unescape(text).replace("\u200b", " ").split())


def statements_in_text(html: bytes) -> bool:
    """Whether the deck has its statement of operations in its text (not only as page images)."""
    try:
        statement_of_operations(document_text(html))
    except ValueError:
        return False
    return True


def title_quarter(title: str) -> pd.Period:
    """The quarter of an Update deck from its title."""
    m = TITLE_RE.search(title)
    if m is None:
        raise ValueError(f"Not the title of a Tesla Update: {title}")
    if m.group("q"):
        return pd.Period(year=int(m.group("qyear")), quarter=int(m.group("q")), freq="Q")
    return pd.Period(year=int(m.group("year")), quarter=ORDINALS[m.group("ordinal")], freq="Q")


def to_value(token: str) -> float:
    if token in ("—", "–", "-"):
        return 0.0
    value = float(token.strip("() ").replace(",", "").replace(" ", ""))
    return -value if token.startswith("(") else value


def values_after(text: str, pos: int, count: int) -> list[float] | None:
    """`count` figures right after a label (footnote marks before the first one are skipped); None if there are fewer."""
    while m := FOOTNOTE_RE.match(text, pos):
        pos = m.end()
    out = []
    for _ in range(count):
        m = VALUE_RE.match(text, pos)
        if m is None:
            return None
        out.append(to_value(m.group(1)))
        pos = m.end()
    return out


def column_date(m: re.Match) -> pd.Timestamp:
    if m.group("q"):
        return pd.Period(year=int(m.group("qyear")), quarter=int(m.group("q")), freq="Q").end_time.normalize()
    if m.group("dday"):
        return pd.Timestamp(year=2000 + int(m.group("dyear")), month=MONTHS[m.group("dmonth")[:3].lower()], day=int(m.group("dday")))
    return pd.Timestamp(year=int(m.group("year")), month=MONTHS[m.group("month")[:3].lower()], day=int(m.group("day")))


def header_before(text: str, pos: int, report: bool = False) -> tuple[list[pd.Timestamp], list[str]]:
    """(the date of each column, the kind of each group: "three", "six", "nine", "twelve", "year") of the header nearest before `pos`: the last run
    of column dates in the text before it. A report (Form 10-Q or 10-K) has one more form, one day over the years ("December 31, 2016 2015 2014")."""
    window_start = max(0, pos - HEADER_SPAN)
    window = text[window_start:pos]
    runs: list[list[re.Match]] = []
    for m in DATE_RE.finditer(window):
        if runs and not window[runs[-1][-1].end() : m.start()].strip(" ,"):
            runs[-1].append(m)
        else:
            runs.append([m])
    days_runs = []  # "Sept 30, June 30, Sept 30, ... 2016 2016 2015": the days, then as many years
    for m in re.finditer(rf"((?:{DAY_RE.pattern}\s*){{2,{MAX_COLUMNS}}})((?:(?:19|20)\d\d\s*){{2,{MAX_COLUMNS}}})", window):
        days = list(DAY_RE.finditer(m.group(1)))
        years = YEAR_RE.findall(m.group(len(m.groups())))
        if len(days) == len(years):
            dates = [pd.Timestamp(year=int(y), month=MONTHS[d.group("month")[:3].lower()], day=int(d.group("day"))) for d, y in zip(days, years)]
            days_runs.append((m.start(), m.end(), dates))
    if report:
        for m in re.finditer(rf"{DAY_RE.pattern}\s*((?:(?:19|20)\d\d\s*){{2,{MAX_COLUMNS}}})", window):
            month, day = MONTHS[m.group("month")[:3].lower()], int(m.group("day"))
            days_runs.append((m.start(), m.end(), [pd.Timestamp(year=int(y), month=month, day=day) for y in YEAR_RE.findall(m.group(3))]))
    candidates = [(r[0].start(), r[-1].end(), [column_date(m) for m in r]) for r in runs if len(r) >= 2]
    candidates += days_runs
    if not candidates:
        raise ValueError(f"No column header before {text[pos : pos + 60]!r}")
    start, end, dates = max(candidates, key=lambda c: c[1])
    kinds = [g.group(0).split()[0].lower().rstrip("s") for g in GROUP_RE.finditer(window[max(0, start - 300) : start])]
    return dates, kinds


def quarter_columns(dates: list[pd.Timestamp], kinds: list[str], report: bool = False) -> tuple[list[pd.Period], bool]:
    """(the quarter of each column used, whether they are three-months columns). Without a group, or with the three-months group alone, all the
    columns; with a year-to-date group after it, the three-months columns (the first three of five, or half of them: a 10-Q's "Three Months Ended
    Six Months Ended June 30, June 30, 2021 2020 2021 2020"). A report's full-year statement (a 10-K: "Year Ended December 31, 2016 2015 2014")
    gives its years, each under its fourth quarter; an Update always has a three-months statement."""
    if report and kinds == ["year"]:
        return [pd.Period(d, freq="Q") for d in dates], False
    if len(kinds) <= 1 and kinds in ([], ["three"]):
        return [pd.Period(d, freq="Q") for d in dates], True
    if len(kinds) == 2 and kinds[0] == "three":
        count = 3 if len(dates) == 5 else len(dates) // 2 if len(dates) % 2 == 0 else None
        if count is not None:
            return [pd.Period(d, freq="Q") for d in dates[:count]], True
    raise ValueError(f"A header with the groups {kinds} and the columns {dates}")


def pick(values: list[float], periods: list[pd.Period], period: pd.Period) -> list[float]:
    """This quarter, the same quarter last year and the previous quarter, as far as the columns have them (the first two are required)."""
    if period not in periods or period - 4 not in periods:
        raise ValueError(f"The columns {periods} lack {period} or {period - 4}")
    return [values[periods.index(p)] for p in (period, period - 4, period - 1) if p in periods]


def row(text: str, pattern: re.Pattern, start: int, end: int, name: str, report: bool = False) -> tuple[list[float], list[pd.Period], bool]:
    """The figures of the first row matching `pattern` between start and end, the quarter of each column used and whether they are
    three-months columns (see quarter_columns)."""
    for m in pattern.finditer(text, start, end):
        dates, kinds = header_before(text, m.start(), report)
        values = values_after(text, m.end(), len(dates))
        if values is not None:
            periods, quarterly = quarter_columns(dates, kinds, report)
            return values[: len(periods)], periods, quarterly
    raise ValueError(f"Not found: {name}")


def statement_of_operations(text: str) -> tuple[int, re.Match]:
    """(where the statement of operations starts, its "Total revenues" row): the first heading followed by a revenue row with the cost of
    revenues after it (the financial summary before it has a revenue row as well, but no cost of revenues)."""
    for heading in STATEMENT_START_RE.finditer(text):
        revenue = REVENUE_RE.search(text, heading.end(), heading.end() + STATEMENT_SPAN)
        if revenue is not None and "cost of revenues" in text[revenue.end() : revenue.end() + 1500].lower():
            return heading.start(), revenue
    raise ValueError("Not found: the statement of operations (its statements are probably page images)")


def parse_tesla_update(html: bytes, non_gaap: str | None = None, report: bool = False) -> Parsed:
    """Parse a Tesla Update. non_gaap is the EPS basis of the stock's Non-GAAP PE: only "diluted" is supported (Tesla's figure today).

    With `report` the document is a Form 10-Q or 10-K (see parse_tesla_filing): its balance sheet comes before the statement of operations,
    a 10-K's statement is for the full year (Parsed.quarterly is False) and there is no Non-GAAP EPS."""
    if non_gaap not in (None, "diluted"):
        raise ValueError(f"Tesla's Update is read for a diluted Non-GAAP EPS only, got {non_gaap}")
    text = document_text(html)
    start, revenue_at = statement_of_operations(text)
    balance = BALANCE_START_RE.search(text, 0 if report else revenue_at.end())
    if balance is None:
        raise ValueError("Not found: the heading of the balance sheet")
    unit = UNIT_RE.search(text, start, revenue_at.start())
    if unit is None:
        raise ValueError("Not found: the unit of the statement of operations")
    scale = 0.001 if unit.group(1).lower() == "thousands" else 1.0
    after = STATEMENT_END_RE.search(text, revenue_at.end())  # the next statement: comprehensive income (a report) or the balance sheet (an Update)
    if after is None:
        raise ValueError("Not found: the end of the statement of operations")
    end = after.start()

    revenue, periods, quarterly = row(text, REVENUE_RE, start, end, "total revenues", report)
    period = max(periods)
    owners = any(m for m in PROFIT_OWNERS_RE.finditer(text, start, end))
    profit, profit_periods, _ = row(text, PROFIT_OWNERS_RE if owners else PROFIT_RE, revenue_at.end(), end, "net income", report)
    eps, eps_periods, _ = row(text, EPS_RE, revenue_at.end(), end, "basic EPS", report)
    shares, shares_periods, _ = row(text, SHARES_RE, revenue_at.end(), end, "basic weighted average shares", report)
    if not profit_periods == eps_periods == shares_periods == periods:
        raise ValueError(f"The rows of the statement of operations have different columns: {periods}, {profit_periods}, {eps_periods}, {shares_periods}")
    values = {
        "revenue": [v * scale for v in pick(revenue, periods, period)],
        "profit": [v * scale for v in pick(profit, periods, period)],
        "eps": pick(eps, periods, period),
        "wa_shares": [v * scale for v in pick(shares, periods, period)],
    }

    equity_at = next((m for m in EQUITY_RE.finditer(text, balance.end()) if values_after(text, m.end(), 2) is not None), None)
    if equity_at is None:
        raise ValueError("Not found: the stockholders' equity of the balance sheet")
    balance_dates, _ = header_before(text, equity_at.start(), report)
    equity = values_after(text, equity_at.end(), len(balance_dates))
    ends = [pd.Period(d, freq="Q") for d in balance_dates]
    year_end = pd.Period(year=period.year - 1, quarter=4, freq="Q")
    if equity is None or period not in ends or year_end not in ends:
        raise ValueError(f"The balance sheet columns {ends} lack {period} or {year_end}")
    values["equity"] = [equity[ends.index(period)] * scale, equity[ends.index(year_end)] * scale]

    notes = {}
    if non_gaap == "diluted" and not report:
        diluted = NON_GAAP_DILUTED_RE.search(text, revenue_at.end())
        if diluted is not None:
            found, found_periods, _ = row(text, NON_GAAP_DILUTED_RE, revenue_at.end(), len(text), "the diluted Non-GAAP EPS")
            values["non_ifrs_eps"] = pick(found, found_periods, period)
            if OLD_DEFINITION_RE.search(text, max(0, diluted.start() - RECONCILIATION_SPAN), diluted.start()):
                notes["non_ifrs_eps"] = OLD_DEFINITION_NOTE
        elif NON_GAAP_BASIC_RE.search(text, revenue_at.end()) is None:
            raise ValueError("Not found: the diluted Non-GAAP EPS")
        # else: the deck states only a basic Non-GAAP EPS (the fourth quarter of 2019 and the first quarter of 2020): no Non-GAAP EPS is taken
    return Parsed("USD", values, period, quarterly, notes or None)


PERIODIC_TITLE_RE = re.compile(r"^Form (?P<form>10-Q|10-K) for the (?:quarterly period|fiscal year) ended (?P<date>[A-Z][a-z]+ \d{1,2}, ?\d{4})$")
PERIODIC_NOTE = "from the Form {form}, because the statements of the Update for this quarter are page images only (the report has no Non-GAAP EPS)"


def parse_tesla_filing(filing, non_gaap: str | None = None) -> Parsed:
    """A Tesla filing: an Update deck, or the Form 10-Q or 10-K of a quarter whose deck has its statements as page images only (its title is
    "Form 10-Q for the quarterly period ended ...", see providers/sec.py). A report is read with the same rows (parse_tesla_update with report):
    the 10-Q's three-months statement, the 10-K's full year (the fourth quarter is the full year minus the first three, see build_history); it has
    no Non-GAAP EPS.
    Its figures carry a note saying where they come from (Parsed.notes)."""
    m = PERIODIC_TITLE_RE.match(filing.title)
    if m is None:
        return parse_tesla_update(filing.content, non_gaap)
    parsed = parse_tesla_update(filing.content, report=True)
    ended = pd.Period(pd.Timestamp(m.group("date")), freq="Q")
    if parsed.period != ended:
        raise ValueError(f"{filing.url}: the statements are for {parsed.period}, the report for {ended}")
    note = PERIODIC_NOTE.format(form=m.group("form"))
    return parsed._replace(notes={k: note for k in ("revenue", "profit", "eps")})
