"""Extract earnings data from TravelSky Technology's results announcements on HKEXnews (PDF, English version): the travelsky_statements layout.

TravelSky (0696.HK) publishes results every half year (interim and annual). Its announcements have two layouts, after the accounting standard used:
  - IFRS (International Financial Reporting Standards), up to the interim results of 2020: an income statement with "Total revenue(s)",
    "Profit (after taxation) attributable to (:) Owner(s) of the Parent/Company", "Basic and diluted (RMB)", negatives in parentheses; the balance
    sheet has "Capital and reserves (or Equity) attributable to owner(s) of the Parent/Company", its total being the last figures before
    "Non-controlling interests".
  - CASBE (China Accounting Standards for Business Enterprises, in an English translation), from the annual results of 2020: an income statement
    numbered like the A-share one, "I. Total operating revenue (or income)", "1. Net profit attributable to shareholders of the parent company (or of the
    Company)", "(I) Basic earnings per share (RMB/share)", negatives written with a minus sign or an en dash; the balance sheet has "Total equity
    attributable to shareholders of the parent company (or of the Company)".
Each row has two figures: this period to date, the same period last year to date (the balance sheet: the end of this period, the end of last year).
Amounts are in RMB thousands and converted to millions; EPS is in yuan. The EPS note gives the weighted average number of ordinary shares in thousands.

The company says the two standards give no material difference for it (2019 net profit attributable to shareholders: RMB 2,542.9 million under IFRS,
2,526.0 million under CASBE; EPS 0.87 under both). The figures of the IFRS filings carry a note saying so (IFRS_NOTE), shown where the calculation is shown.
"""

from __future__ import annotations

import re

AMOUNT = r"(?:\(\d{1,3}(?:,\d{3})+\)|[-–]?\d{1,3}(?:,\d{3})+)(?![\d,])"  # an amount in thousands: always has a thousands separator, so a note number is never taken
EPS = r"(?:\(\d+\.\d+\)|[-–]?\d+\.\d+)(?![\d.])"
NOTE = r"(?:\d{1,2}(?:\([a-z]\)|\. ?[a-z][)\x81]?)?)"  # note references: 3, 4(b), 5.j), 5. j); the 2025 interim's ")" is extracted as \x81
UNIT_RE = re.compile(r"RMB\W{0,2}000")  # RMB’000 or RMB'000 in the table header

IFRS_REVENUE_RE = re.compile(rf"\bTotal revenues? (?:{NOTE} )?(?P<a>{AMOUNT}) (?P<b>{AMOUNT})")
IFRS_PROFIT_RE = re.compile(rf"(?:\(Loss\)/profit|Profit) (?:after taxation )?attributable to:? Owners? of the (?:Parent|Company) (?P<a>{AMOUNT}) (?P<b>{AMOUNT})")
IFRS_EPS_RE = re.compile(rf"\bBasic and diluted \(RMB\) (?:{NOTE} )?(?P<a>{EPS}) (?P<b>{EPS})")
IFRS_EQUITY_RE = re.compile(r"(?:Capital and reserves|Equity) attributable to (?:Owners?|owners) of the (?:Parent|Company)(?P<body>.*?)Non-controlling interests")

CASBE_REVENUE_RE = re.compile(rf"\bI\. Total operating (?:revenue|income) (?:{NOTE} )?(?P<a>{AMOUNT}) (?P<b>{AMOUNT})")
CASBE_PROFIT_RE = re.compile(
    rf"\bNet profit attributable to shareholders of the (?:parent company|Company) (?:\(“[-–]” for net loss\) )?(?P<a>{AMOUNT}) (?P<b>{AMOUNT})"
)
CASBE_EPS_RE = re.compile(rf"\(I\) Basic earnings per share \(RMB(?:/share)?\) (?:{NOTE} )?(?P<a>{EPS}) (?P<b>{EPS})")
CASBE_EQUITY_RE = re.compile(rf"\bTotal equity attributable to shareholders of the (?:parent company|Company) (?P<a>{AMOUNT}) (?P<b>{AMOUNT})")
# preference shares and perpetual bonds would have to be taken off the equity of ordinary shareholders: none so far, and a figure there is an error
OTHER_EQUITY_RE = re.compile(rf"Shareholders’ equity:.*?Other equity instruments (?P<a>{AMOUNT}|[–—-]) (?P<b>{AMOUNT}|[–—-])")

WA_SHARES_RE = re.compile(rf"[Ww]eighted average number of ordinary shares in issue\)? (?P<a>{AMOUNT}) (?P<b>{AMOUNT})")

IFRS = "IFRS"
CASBE = "CASBE"
RULES = {  # standard -> (revenue, profit, EPS) rows of the income statement
    IFRS: (IFRS_REVENUE_RE, IFRS_PROFIT_RE, IFRS_EPS_RE),
    CASBE: (CASBE_REVENUE_RE, CASBE_PROFIT_RE, CASBE_EPS_RE),
}
IFRS_NOTE = (
    "under IFRS (from the 2020 annual results TravelSky reports under CASBE, China Accounting Standards for Business Enterprises, "
    "which the company states makes no material difference)"
)
# no "; " in a note: the saved note column joins the notes with it
NOTED_SERIES = ("eps", "revenue", "profit", "equity")


def to_number(token: str) -> float:
    negative = token.startswith(("(", "-", "–"))
    value = float(token.strip("()-–").replace(",", ""))
    return -value if negative else value


def _pair(m: re.Match, scale: float = 1.0) -> list[float]:
    return [to_number(m.group("a")) * scale, to_number(m.group("b")) * scale]


def parse_travelsky_statements(pages) -> tuple[str, dict[str, list[float]], dict[str, str]]:
    """Revenue, net profit attributable to shareholders, basic EPS, their equity and the weighted average shares of one announcement.

    `pages` is the filing's page texts (filings.Pages). Returns (currency, values, series notes). The income statement is the first page with a revenue
    row of either standard; the net profit row is the one before the EPS row of the statement; equity is taken from the balance sheet.
    An error is raised when a figure is missing; another row is never taken instead.
    """
    standard = start = None
    for i in range(len(pages)):
        found = next((s for s, (revenue_re, _, _) in RULES.items() if revenue_re.search(pages[i])), None)
        if found:
            standard, start = found, i
            break
    if standard is None:
        raise ValueError("The consolidated income statement was not found")
    revenue_re, profit_re, eps_re = RULES[standard]

    if not any(UNIT_RE.search(pages[i]) for i in range(len(pages))):  # in the table headers; the 2015-2018 interim statements lose theirs in the extracted text
        raise ValueError("The amounts are not in RMB thousands")
    scale = 0.001  # thousands -> millions

    values: dict[str, list[float]] = {}
    for i in range(start, len(pages)):
        text = pages[i]
        if "revenue" not in values and (m := revenue_re.search(text)):
            values["revenue"] = _pair(m, scale)
        if "eps" not in values:
            eps = eps_re.search(text)
            if "profit" not in values and (m := profit_re.search(text[: eps.start()] if eps else text)):
                values["profit"] = _pair(m, scale)
            if eps:
                if "profit" not in values:
                    raise ValueError("The net profit row was not found before the EPS row")
                values["eps"] = _pair(eps)
        if {"revenue", "profit", "eps"} <= values.keys():
            break
    for i in range(len(pages)):  # the balance sheet: after the income statement in the annual results, before it in the interim results
        text = pages[i]
        if standard == IFRS and (m := IFRS_EQUITY_RE.search(text)):
            amounts = re.findall(AMOUNT, m.group("body"))
            if len(amounts) >= 2:
                values["equity"] = [to_number(amounts[-2]) * scale, to_number(amounts[-1]) * scale]
                break
        elif standard == CASBE and (m := CASBE_EQUITY_RE.search(text)):
            other = OTHER_EQUITY_RE.search(text)
            if other and any(re.fullmatch(AMOUNT, other.group(k)) for k in ("a", "b")):
                raise ValueError("The balance sheet has other equity instruments (preference shares or perpetual bonds)")
            values["equity"] = _pair(m, scale)
            break
    missing = {"revenue", "profit", "eps", "equity"} - values.keys()
    if missing:
        raise ValueError(f"Not found: {sorted(missing)}")

    for i in range(len(pages)):
        if m := WA_SHARES_RE.search(pages[i]):
            values["wa_shares"] = [x / 1000 for x in _pair(m)]  # thousands of shares -> millions
            break
    notes = {s: IFRS_NOTE for s in NOTED_SERIES} if standard == IFRS else {}
    return "CNY", values, notes
