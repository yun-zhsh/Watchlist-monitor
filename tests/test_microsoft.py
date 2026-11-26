"""Microsoft (MSFT): 8-K press releases in the us_press_release layout, with the dollar sign in the cell of the number ("$20,531"), a fiscal year
ending June 30, and headlines without "announces" or "reports" ("Microsoft Cloud Strength Highlights Second Quarter Results")."""

import json
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.config import load_config
from valuation_monitor.providers.sec import headline
from valuation_monitor.us_release import parse_us_release, without_dollar_signs

ROOT = Path(__file__).resolve().parent.parent
MSFT = ROOT / "data" / "filings" / "MSFT"


def table(*rows):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def release(*tables, unit="(In millions, except per share amounts) (Unaudited)"):
    return ("<html><body><p>Exhibit 99.1</p>" + "".join(f"<p>{unit}</p>{t}" for t in tables) + "</body></html>").encode()


INCOME = table(  # the dollar sign is in the cell of the number, as in every Microsoft release
    ["Three Months Ended September 30,"],
    ["2026", "2025"],
    ["Revenue:"],
    ["Product", "$14,631", "$15,922"],
    ["Service and other", "77,648", "61,751"],
    ["Total revenue", "92,279", "77,673"],
    ["Operating income", "45,915", "37,961"],
    ["Net income", "$36,629", "$27,747"],
    ["Earnings per share:"],
    ["Basic", "$4.93", "$3.73"],
    ["Diluted", "$4.92", "$3.72"],
    ["Weighted average shares outstanding:"],
    ["Basic", "7,426", "7,433"],
    ["Diluted", "7,446", "7,466"],
)
BALANCE = table(  # the comparison column is the end of the fiscal year, June 30
    ["September 30, 2026", "June 30, 2026"],
    ["Total assets", "$779,403", "$758,376"],
    ["Total stockholders' equity", "470,509", "442,387"],
    ["Total liabilities and stockholders' equity", "$779,403", "$758,376"],
)


def test_dollar_signs_in_the_cells_are_read():
    p = parse_us_release(release(INCOME, BALANCE))
    assert p.period == pd.Period("2026Q3") and p.quarterly and p.currency == "USD"
    assert p.values["revenue"] == [92279.0, 77673.0] and p.values["profit"] == [36629.0, 27747.0]
    assert p.values["eps"] == [4.93, 3.73] and p.values["wa_shares"] == [7426.0, 7433.0]
    assert p.values["equity"] == [470509.0]  # only this quarter's: the June 30 column is not the previous December
    assert "non_ifrs_eps" not in p.values


def test_a_loss_and_a_fiscal_fourth_quarter():
    income = (
        INCOME.replace("September 30,", "June 30,").replace(">2026<", ">2015<").replace(">2025<", ">2014<")
        .replace("$36,629", "$(3,195)").replace("$4.93", "$(0.40)")
    )
    twelve = income.replace("Three Months Ended June 30,", "Twelve Months Ended June 30,")
    balance = BALANCE.replace("September 30, 2026", "June 30, 2015").replace("June 30, 2026", "June 30, 2014")
    p = parse_us_release(release(income, twelve, balance))
    assert p.period == pd.Period("2015Q2") and p.quarterly
    assert p.values["profit"] == [-3195.0, 27747.0] and p.values["eps"] == [-0.40, 3.73]
    assert p.values["equity"] == [470509.0]


def test_without_dollar_signs():
    rows = [["Net income", "$36,629", "$(3,195)", "($0.40)", "$ 12", "-", "$", "Revenue $5 billion"]]
    assert without_dollar_signs(rows) == [["Net income", "36,629", "(3,195)", "(0.40)", "12", "-", "$", "Revenue $5 billion"]]


def test_other_releases_are_read_as_before():
    """A release whose statements are found as they are is not read again without the dollar signs (the separate "$" cells of other companies
    are dropped by table_rows anyway)."""
    income = INCOME.replace("$", "</td><td>$</td><td>")
    balance = BALANCE.replace("$", "</td><td>$</td><td>")
    assert parse_us_release(release(income, balance)).values == parse_us_release(release(INCOME, BALANCE)).values


def lead(title: str, body: str = "REDMOND, Wash. — October 28, 2026 — Microsoft Corp. today announced the following results for the quarter ended September 30, 2026") -> bytes:
    return f"<html><body><p>EX-99.1 2 msft-ex99_1.htm EX-99.1</p><p>Exhibit 99.1</p><p>{title}</p><p>{body}</p></body></html>".encode()


@pytest.mark.parametrize(
    "title",
    [
        "Microsoft Cloud Strength Highlights Second Quarter Results",
        "Microsoft Cloud and AI Strength Fuels Fourth Quarter Results",
        "Microsoft Cloud Highlights Q3 Results",
        "Microsoft Cloud and Hardware Results Drives Fourth Quarter Performance",
        "Microsoft First Quarter Results",
        "Microsoft Cloud Continues to Grow, Powers First Quarter Results",
    ],
)
def test_microsoft_headlines(title):
    assert headline(lead(title)) == title


def test_microsoft_headline_needs_the_results_sentence():
    assert headline(lead("Microsoft Announces First Quarter Results")) == "Microsoft Announces First Quarter Results"  # the usual rule
    assert headline(lead("Microsoft Cloud Strength Drives Fourth Quarter Results", "Microsoft will hold its annual shareholder meeting")) is None
    assert headline(lead("Microsoft to Webcast First Quarter Results", "REDMOND, Wash. — Microsoft Corp. will publish its results")) is None


def test_config():
    stocks = load_config(ROOT / "config.yaml").stocks
    stock = next(s for s in stocks if s.symbol == "MSFT")
    assert stock.name == "Microsoft" and stock.cik == 789019 and stock.sec_form == "8-K" and stock.filing_format == "us_press_release"
    assert stock.metrics == ["pe", "ps", "pb"] and stock.eps_basis == {}
    assert stock.fiscal_year_end == 6


def test_fiscal_labels():
    """The fiscal year ends June 30 and is named after the calendar year it ends in."""
    from valuation_monitor.filings import period_label

    labels = {q: period_label(pd.Period(q), 4, 6) for q in ("2026Q3", "2026Q2", "2025Q4", "2014Q4")}
    assert labels == {"2026Q3": "FY2027Q1", "2026Q2": "FY2026Q4", "2025Q4": "FY2026Q2", "2014Q4": "FY2015Q2"}


saved = pytest.mark.skipif(not (MSFT / "index.json").exists(), reason="no saved Microsoft filings (the scheduled update saves them)")


def parsed_releases():
    index = [e for e in json.loads((MSFT / "index.json").read_text(encoding="utf-8")) if e["results"]]
    return {p.period: p for p in (parse_us_release((MSFT / e["file"]).read_bytes()) for e in index)}


@saved
def test_every_saved_microsoft_release_parses():
    by_period = parsed_releases()
    periods = sorted(by_period)
    assert periods[0] <= pd.Period("2014Q4") and periods[-1] >= pd.Period("2026Q3")
    assert periods == list(pd.period_range(periods[0], periods[-1], freq="Q"))
    for period, p in by_period.items():
        assert p.currency == "USD" and p.quarterly, period
        assert len(p.values["equity"]) == 1, period  # the comparison column is always June 30
        for col in (0, 1):
            profit, eps, shares = p.values["profit"][col], p.values["eps"][col], p.values["wa_shares"][col]
            assert abs(profit / shares / eps - 1) < 0.01, (period, col)


@saved
def test_prior_year_columns_agree_except_the_restatements():
    """Microsoft restated fiscal 2016 for the early adoption of the share-based payments standard (ASU 2016-09, from the first quarter of
    fiscal 2017) and fiscal 2016-2017 for the full retrospective adoption of the revenue standard (ASC 606, from the first quarter of fiscal
    2018, which removed the Windows 10 revenue deferral); everything else agrees with the release a year later."""
    by_period = parsed_releases()
    differing = set()
    for period, p in by_period.items():
        later = by_period.get(period + 4)
        if later is None:
            continue
        for key in ("revenue", "profit", "eps", "wa_shares"):
            if abs(p.values[key][0] - later.values[key][1]) > (0.006 if key == "eps" else 0.5):
                differing.add(str(period))
    assert differing == {"2015Q3", "2015Q4", "2016Q3", "2016Q4", "2017Q1", "2017Q2"}


@saved
def test_microsoft_known_values():
    by_period = parsed_releases()
    q = by_period[pd.Period("2026Q3")]
    assert q.values["revenue"] == [92279.0, 77673.0] and q.values["eps"] == [4.93, 3.73] and q.values["equity"] == [470509.0]
    assert by_period[pd.Period("2015Q2")].values["eps"] == [-0.40, 0.56]  # the Nokia phone business impairment
    assert by_period[pd.Period("2017Q4")].values["eps"] == [-0.82, 0.81]  # the US tax reform charge
