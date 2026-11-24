"""Dynatrace (DT): 8-K press releases in the us_press_release layout, with a fiscal year ending March 31, the LLC labels before the
August 2019 IPO, single shares under a "dollars in thousands" note, and the basic Non-GAAP EPS of the GAAP to Non-GAAP reconciliation."""

import json
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.config import load_config
from valuation_monitor.us_release import parse_us_release

ROOT = Path(__file__).resolve().parent.parent
DT = ROOT / "data" / "filings" / "DT"


def table(*rows):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def release(*tables, unit="(Unaudited - In thousands, except per share amounts)"):
    return ("<html><body><p>Exhibit 99.1</p>" + "".join(f"<p>{unit}</p>{t}" for t in tables) + "</body></html>").encode()


INCOME = table(
    ["Three Months Ended September 30,", "Six Months Ended September 30,"],
    ["2026", "2025", "2026", "2025"],
    ["Total revenue", "579,918", "493,849", "1,134,466", "971,198"],
    ["Net income", "67,764", "57,243", "104,415", "105,198"],
    ["Net income per share:"],
    ["Basic", "0.24", "0.19", "0.36", "0.35"],
    ["Diluted", "0.22", "0.19", "0.35", "0.35"],
    ["Weighted-average shares outstanding:"],
    ["Basic", "288,025", "301,558", "290,099", "300,859"],
    ["Diluted", "301,671", "304,303", "297,727", "304,801"],
)
BALANCE = table(
    ["September 30, 2026", "March 31, 2026"],
    ["Total assets", "4,416,412", "4,720,000"],
    ["Total shareholders' equity", "2,273,691", "2,611,390"],
    ["Total liabilities and shareholders' equity", "4,416,412", "4,720,000"],
)
RECONCILIATION = table(
    ["Three Months Ended September 30,", "Six Months Ended September 30,"],
    ["2026", "2025", "2026", "2025"],
    ["Non-GAAP net income:"],
    ["Net income", "67,764", "57,243", "104,415", "105,198"],
    ["Non-GAAP net income", "162,028", "134,226", "301,522", "262,077"],
    ["Non-GAAP net income per share:"],
    ["Non-GAAP net income per share - basic", "0.56", "0.44", "1.04", "0.86"],
    ["Non-GAAP net income per share - diluted", "0.54", "0.44", "1.01", "0.85"],
)
SUMMARY = table(  # the financial summary at the top: its Non-GAAP EPS is on diluted shares and is not taken
    ["Three Months Ended September 30,"],
    ["2026", "2025"],
    ["Non-GAAP net income per share - diluted", "0.54", "0.44"],
    ["Non-GAAP net income per share", "0.54", "0.44"],
)


def test_the_basic_non_gaap_eps_and_only_this_quarters_balance_when_the_year_ends_in_march():
    p = parse_us_release(release(SUMMARY, INCOME, BALANCE, RECONCILIATION), "basic")
    assert p.period == pd.Period("2026Q3") and p.quarterly and p.currency == "USD"
    assert p.values["non_ifrs_eps"] == [0.56, 0.44]  # basic, not the diluted 0.54 of the summary
    assert p.values["equity"] == pytest.approx([2273.691])  # March 31 is not the previous December: it is filed under its own quarter's release
    assert p.values["wa_shares"] == pytest.approx([288.025, 301.558]) and p.values["eps"] == [0.24, 0.19]


def test_a_release_without_a_basic_non_gaap_eps_has_none():
    p = parse_us_release(release(SUMMARY, INCOME, BALANCE), "basic")
    assert "non_ifrs_eps" not in p.values


def test_the_basic_non_gaap_eps_is_read_only_for_a_stock_with_a_basic_non_gaap_pe():
    """Meta, Cognizant and Sea have no Non-GAAP PE (non_gaap None) and Amphenol's is diluted: none of them reads this row."""
    html = release(SUMMARY, INCOME, BALANCE, RECONCILIATION)
    assert "non_ifrs_eps" not in parse_us_release(html).values
    assert "non_ifrs_eps" not in parse_us_release(html, "diluted").values  # Amphenol's Adjusted Diluted EPS is not in this release


def test_basic_non_gaap_eps_of_two_tables_must_agree():
    other = RECONCILIATION.replace(">0.56<", ">0.57<")
    with pytest.raises(ValueError, match="Non-GAAP EPS"):
        parse_us_release(release(INCOME, BALANCE, RECONCILIATION, other), "basic")


def test_the_llc_before_the_ipo_member_deficit_and_single_shares_under_a_dollars_in_thousands_note():
    """The first release (quarter to June 30, 2019): "Total member's deficit", and shares in single shares, as the note gives a unit for dollars only."""
    income = table(
        ["Three Months Ended June 30,"],
        ["2019", "2018"],
        ["Total revenue", "122,550", "98,221"],
        ["Net loss", "(49,155", "(23,556"],
        ["Net loss per share:"],
        ["Basic and diluted", "(0.21", "(0.10"],
        ["Weighted average shares outstanding:"],
        ["Basic and diluted", "237,693,127", "233,970,804"],
    )
    balance = table(
        ["June 30, 2019", "March 31, 2019"],
        ["Total assets", "1,775,583", "1,811,366"],
        ["Member's deficit:"],
        ["Accumulated deficit", "(225,157", "(176,002"],
        ["Total member’s deficit", "(437,629", "(390,258"],
        ["Total liabilities and member’s deficit", "1,775,583", "1,811,366"],
    )
    p = parse_us_release(release(income, balance, unit="(Unaudited – dollars in thousands, except per share amounts)"))
    assert p.period == pd.Period("2019Q2")
    assert p.values["wa_shares"] == pytest.approx([237.693127, 233.970804])
    assert p.values["equity"] == pytest.approx([-437.629])
    assert p.values["profit"] == pytest.approx([-49.155, -23.556])


def test_shareholders_equity_or_member_deficit_after_the_ipo():
    balance = table(
        ["December 31, 2019", "March 31, 2019"],
        ["Total assets", "1,988,307", "1,811,366"],
        ["Total shareholders' equity / member's deficit", "899,190", "(390,258"],
        ["Total liabilities and shareholders' equity / member's deficit", "1,988,307", "1,811,366"],
    )
    income = INCOME.replace("September 30,", "December 31,").replace("Six Months", "Nine Months").replace(">2026<", ">2019<").replace(">2025<", ">2018<")
    p = parse_us_release(release(income, balance))
    assert p.values["equity"] == pytest.approx([899.19])


def test_config_has_both_pes_on_basic_eps():
    stock = next(s for s in load_config(ROOT / "config.yaml").stocks if s.symbol == "DT")
    assert stock.cik == 1773383 and stock.filing_format == "us_press_release" and stock.name == "Dynatrace"
    assert stock.metrics == ["pe", "pe_non_gaap", "ps", "pb"] and stock.eps_basis == {}
    names = [s.name for s in load_config(ROOT / "config.yaml").stocks]
    assert names.index("Cognizant Technology Solutions") < names.index("Amphenol") < names.index("Dynatrace") < names.index("ASML Holding (NY registry shares)")


saved = pytest.mark.skipif(not (DT / "index.json").exists(), reason="no saved Dynatrace filings (the scheduled update saves them)")


def parsed_releases():
    index = [e for e in json.loads((DT / "index.json").read_text(encoding="utf-8")) if e["results"]]
    return {p.period: p for p in (parse_us_release((DT / e["file"]).read_bytes(), "basic") for e in index)}


@saved
def test_every_saved_dynatrace_release_parses():
    by_period = parsed_releases()
    periods = sorted(by_period)
    assert periods[0] == pd.Period("2019Q2") and periods[-1] >= pd.Period("2026Q3")
    assert periods == list(pd.period_range(periods[0], periods[-1], freq="Q"))  # no quarter missing (two exhibits are named without "ex99")
    for period, p in by_period.items():
        assert p.currency == "USD" and p.quarterly, period
        assert len(p.values["non_ifrs_eps"]) == 2, period  # every release states a basic Non-GAAP EPS
        for col in (0, 1):
            profit, eps, shares = p.values["profit"][col], p.values["eps"][col], p.values["wa_shares"][col]
            assert abs(profit / shares - eps) <= 0.005 + 1e-9, (period, col)  # basic EPS = net profit / shares, rounded to the cent
        assert len(p.values["equity"]) == 1, period  # the comparison column is always March 31, never the previous December


@saved
def test_prior_year_columns_agree_except_dynatraces_revisions():
    """The March quarter 2020 net income was revised (FY2020 tax provision), and the Non-GAAP EPS of three 2020 quarters after the May 2021
    change to an effective cash tax; everything else agrees with the release a year later."""
    by_period = parsed_releases()
    differing = set()
    for period, p in by_period.items():
        later = by_period.get(period + 4)
        if later is None:
            continue
        for key in ("revenue", "profit", "eps", "non_ifrs_eps", "wa_shares"):
            tolerance = 0.006 if "eps" in key else 0.5
            if abs(p.values[key][0] - later.values[key][1]) > tolerance:
                differing.add((str(period), key))
    assert differing == {("2020Q1", "profit"), ("2020Q1", "eps"), ("2020Q1", "non_ifrs_eps"), ("2020Q2", "non_ifrs_eps"), ("2020Q3", "non_ifrs_eps")}


@saved
def test_dynatrace_known_values():
    by_period = parsed_releases()
    q = by_period[pd.Period("2026Q3")]
    assert q.values["revenue"] == pytest.approx([579.918, 493.849]) and q.values["non_ifrs_eps"] == [0.56, 0.44]
    assert q.values["equity"] == pytest.approx([2273.691])
    assert by_period[pd.Period("2019Q2")].values["equity"] == pytest.approx([-437.629])  # the LLC's member's deficit before the IPO
