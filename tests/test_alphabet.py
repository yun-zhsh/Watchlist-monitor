"""Alphabet (GOOGL): 8-K press releases in the us_press_release layout, with Alphabet's own labels: "Quarter Ended" and "Year To Date"
groups (from the fourth quarter of 2019), EPS rows per share class, share counts in thousands (fourth quarter of 2019 to first quarter of 2022),
no share counts at all (until the third quarter of 2019) and a balance sheet split over two tables (until the fourth quarter of 2016)."""

import json
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.config import load_config
from valuation_monitor.us_release import parse_us_release

ROOT = Path(__file__).resolve().parent.parent
GOOGL = ROOT / "data" / "filings" / "GOOGL"


def table(*rows):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def release(*parts):
    return ("<html><body><p>Exhibit 99.1</p>" + "".join(parts) + "</body></html>").encode()


UNIT = "<p>(In millions, except per share amounts; unaudited)</p>"
BALANCE = table(
    ["As of December 31, 2025", "As of September 30, 2026"],
    ["(unaudited)"],
    ["Total assets", "595,281", "1,003,312"],
    ["Total stockholders' equity", "415,265", "677,549"],
    ["Total liabilities and stockholders' equity", "595,281", "1,003,312"],
)
INCOME_2026 = table(
    ["Quarter Ended September 30,", "Year To Date September 30,"],
    ["2025", "2026", "2025", "2026"],
    ["Revenues", "102,346", "127,577", "289,007", "357,269"],
    ["Net income", "34,979", "26,468", "97,715", "201,239"],
    ["Preferred stock dividends", "0", "298", "0", "384"],
    ["Net income available to common stockholders", "34,979", "26,170", "97,715", "200,855"],
    ["Basic net income per common share", "2.89", "2.14", "8.06", "16.51"],
    ["Diluted net income per common share", "2.87", "2.12", "7.99", "16.33"],
    ["Number of shares used in basic net income per common share calculation", "12,086", "12,256", "12,130", "12,169"],
    ["Number of shares used in diluted net income per common share calculation", "12,203", "12,368", "12,230", "12,324"],
)


def test_quarter_ended_and_year_to_date_groups_and_the_number_of_shares_row():
    p = parse_us_release(release(UNIT, BALANCE, UNIT, INCOME_2026))
    assert p.period == pd.Period("2026Q3") and p.quarterly and p.currency == "USD"
    assert p.values["revenue"] == [127577.0, 102346.0]
    assert p.values["profit"] == [26468.0, 34979.0]  # the net income row, not the one available to common stockholders
    assert p.values["eps"] == [2.14, 2.89] and p.values["wa_shares"] == [12256.0, 12086.0]
    assert p.values["equity"] == [677549.0, 415265.0]


def test_a_fourth_quarter_takes_the_quarter_not_the_year_ended_group():
    income = table(
        ["Quarter Ended December 31,", "Year Ended December 31,"],
        ["2024", "2025", "2024", "2025"],
        ["Revenues", "96,469", "113,828", "350,018", "402,836"],
        ["Net income", "26,536", "34,455", "100,118", "132,170"],
        ["Basic net income per share", "2.17", "2.85", "8.13", "10.91"],
        ["Number of shares used in basic net income per share calculation", "12,228", "12,073", "12,312", "12,117"],
    )
    balance = table(["As of December 31, 2024", "As of December 31, 2025"], ["Total stockholders' equity", "325,084", "415,265"])
    p = parse_us_release(release(UNIT, balance, UNIT, income))
    assert p.period == pd.Period("2025Q4") and p.values["eps"] == [2.85, 2.17] and p.values["wa_shares"] == [12073.0, 12228.0]


def test_share_amounts_reflected_in_thousands():
    income = table(
        ["Quarter Ended March 31,"],
        ["2021", "2022"],
        ["Revenues", "55,314", "68,011"],
        ["Net income", "17,930", "16,436"],
        ["Basic earnings per share of Class A, Class B, and Class C stock", "26.63", "24.90"],
        ["Diluted earnings per share of Class A, Class B, and Class C stock", "26.29", "24.62"],
        ["Number of shares used in basic earnings per share calculation", "673,220", "660,171"],
    )
    balance = table(["As of December 31, 2021", "As of March 31, 2022"], ["Total stockholders' equity", "251,635", "254,004"])
    unit = "<p>(In millions, except share amounts which are reflected in thousands and per share amounts)</p>"
    p = parse_us_release(release(UNIT, balance, unit, income))
    assert p.period == pd.Period("2022Q1") and p.values["eps"] == [24.9, 26.63]
    assert p.values["wa_shares"] == pytest.approx([660.171, 673.220])  # thousands -> millions


def test_without_share_counts_the_statement_is_taken_only_when_no_table_has_them():
    income = table(
        ["Three Months Ended"],
        ["March 31,"],
        ["2016", "2017"],
        ["(unaudited)"],
        ["Revenues", "20,257", "24,750"],
        ["Net income", "4,207", "5,426"],
        ["Basic net income per share of Class A and B common stock and Class C capital stock", "6.12", "7.85"],
        ["Diluted net income per share of Class A and B common stock and Class C capital stock", "6.02", "7.73"],
    )
    balance = table(["December 31, 2016", "March 31, 2017"], ["Total stockholders' equity", "139,036", "144,949"])
    p = parse_us_release(release(UNIT, balance, UNIT, income))
    assert p.period == pd.Period("2017Q1") and p.values["eps"] == [7.85, 6.12] and "wa_shares" not in p.values
    assert p.values["equity"] == [144949.0, 139036.0]
    # a table with share counts wins over one without: the fallback is not used when the shares are disclosed
    with_shares = table(
        ["Three Months Ended"],
        ["March 31,"],
        ["2016", "2017"],
        ["Revenues", "20,257", "24,750"],
        ["Net income", "4,207", "5,426"],
        ["Basic net income per share of Class A and B common stock and Class C capital stock", "6.12", "7.85"],
        ["Number of shares used in basic net income per share calculation", "687", "691"],
    )
    p = parse_us_release(release(UNIT, balance, UNIT, income, UNIT, with_shares))
    assert p.values["wa_shares"] == [691.0, 687.0]


def test_separate_class_rows_take_class_a_and_b_and_the_balance_sheet_continued_in_a_second_table():
    """2015-2016: the classes have rows of their own (Class C differs in 2015 because of the Adjustment Payment), the per-share headings with a
    colon have no figures, and the equity rows are in a table of their own after the one with the dates."""
    income = table(
        ["Three Months Ended", "Six Months Ended"],
        ["June 30,", "June 30,"],
        ["2015", "2016", "2015", "2016"],
        ["Revenues", "17,727", "21,500", "34,985", "41,757"],
        ["Net income", "3,931", "4,877", "7,446", "9,084"],
        ["Less: Adjustment Payment to Class C capital stockholders", "522", "0", "522", "0"],
        ["Net income available to all stockholders", "3,409", "4,877", "6,924", "9,084"],
        ["Basic net income (loss) per share of Class A and B common stock:"],
        ["Basic net income per share of Class A and B common stock", "4.99", "7.11", "10.15", "13.23"],
        ["Basic net income per share of Class C capital stock", "6.51", "7.11", "11.68", "13.23"],
    )
    assets = table(["As of December 31, 2015", "As of June 30, 2016"], ["(unaudited)"], ["Total assets", "147,461", "154,315"])
    equity = table(
        ["Stockholders' equity:"],
        ["Retained earnings", "89,223", "96,233"],
        ["Total stockholders' equity", "120,331", "127,879"],
        ["Total liabilities and stockholders' equity", "147,461", "154,315"],
    )
    p = parse_us_release(release(UNIT, assets, equity, UNIT, income))
    assert p.period == pd.Period("2016Q2") and p.values["eps"] == [7.11, 4.99]  # Class A and B
    assert p.values["equity"] == [127879.0, 120331.0]


def test_config_has_no_non_gaap_pe_and_comes_first():
    stocks = load_config(ROOT / "config.yaml").stocks
    stock = next(s for s in stocks if s.symbol == "GOOGL")
    assert stock.cik == 1652044 and stock.filing_format == "us_press_release" and stock.name == "Alphabet" and stock.sec_form == "8-K"
    assert stock.metrics == ["pe", "ps", "pb"] and stock.eps_basis == {}
    symbols = [s.symbol for s in stocks]
    assert symbols.index("GOOGL") < symbols.index("META")


saved = pytest.mark.skipif(not (GOOGL / "index.json").exists(), reason="no saved Alphabet filings (the scheduled update saves them)")


def parsed_releases():
    index = [e for e in json.loads((GOOGL / "index.json").read_text(encoding="utf-8")) if e["results"]]
    return {p.period: (e, p) for e, p in ((e, parse_us_release((GOOGL / e["file"]).read_bytes())) for e in index)}


SPLIT_RELEASE = pd.Timestamp("2022-07-18")  # the 20-for-1 split: releases from then on are on the new share basis


def basis(entry) -> float:
    return 20.0 if pd.Timestamp(entry["published"]) < SPLIT_RELEASE else 1.0


@saved
def test_every_saved_alphabet_release_parses():
    by_period = parsed_releases()
    periods = sorted(by_period)
    assert periods[0] == pd.Period("2015Q3") and periods[-1] >= pd.Period("2026Q3")
    assert periods == list(pd.period_range(periods[0], periods[-1], freq="Q"))
    for period, (entry, p) in by_period.items():
        assert p.currency == "USD" and p.quarterly and len(p.values["equity"]) == 2, period
        assert ("wa_shares" in p.values) == (period >= pd.Period("2019Q4")), period  # no share counts before the fourth quarter of 2019
        if "wa_shares" in p.values:
            profit, eps, shares = p.values["profit"][0], p.values["eps"][0], p.values["wa_shares"][0]
            # net income / basic EPS within 1% of the shares: the preferred dividends of 2026 (USD 298 million in 2026Q3) are the largest gap
            assert abs(profit / eps / shares - 1) < 0.01, period


@saved
def test_prior_year_columns_agree_on_the_split_adjusted_basis():
    """Every quarter's revenue, net income and EPS agree between its own release and the prior-year column a year later (EPS divided by 20 for
    the releases before the July 2022 split, rounded to the cent by Alphabet on the new basis)."""
    by_period = parsed_releases()
    for period, (entry, p) in by_period.items():
        if (later := by_period.get(period + 4)) is None:
            continue
        later_entry, q = later
        assert p.values["revenue"][0] == q.values["revenue"][1] and p.values["profit"][0] == q.values["profit"][1], period
        assert abs(p.values["eps"][0] / basis(entry) - q.values["eps"][1] / basis(later_entry)) <= 0.0051, period


@saved
def test_alphabet_known_values():
    by_period = parsed_releases()
    _, q = by_period[pd.Period("2026Q3")]
    assert q.values["eps"] == [2.14, 2.89] and q.values["wa_shares"] == [12256.0, 12086.0] and q.values["equity"] == [677549.0, 415265.0]
    _, q = by_period[pd.Period("2020Q1")]
    assert q.values["wa_shares"] == pytest.approx([686.465, 694.869])  # thousands of shares, converted
    _, q = by_period[pd.Period("2015Q3")]
    assert q.values["eps"] == [5.8, 4.05] and q.values["equity"] == [116241.0, 103860.0]  # the total of continuing and discontinued operations
