"""IFRS consolidated financial statements in HTML furnished on Form 6-K (XP Inc., thousands of Brazilian reais)."""

import json
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.ifrs import parse_ifrs_statements

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "data" / "filings" / "XP"


def table(*rows):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def statements(heading, *tables, unit="In thousands of Brazilian Reais"):
    return f"<html><body><p>EX-99.1 Exhibit 99.1</p><p>{heading}</p><p>{unit}</p>{''.join(tables)}</body></html>".encode()


INTERIM_HEADING = "XP Inc. Interim condensed consolidated financial statements at September 30, 2026 and report on review"
INTERIM_BALANCE = table(
    ["Unaudited interim condensed consolidated balance sheets", "In thousands of Brazilian Reais"],
    ["Liabilities and equity", "Note", "September 30, 2026", "December 31, 2025"],
    ["Total liabilities", "405,863,079", "372,980,645"],
    ["Equity attributable to owners of the Parent company", "18", "26,200,077", "23,546,701"],
    ["Non-controlling interest", "602", "1,007"],
    ["Total equity", "26,200,679", "23,547,708"],
)
INTERIM_INCOME = table(
    ["Nine months periods ended September 30,", "Three months period ended September 30,"],
    ["Note", "2026", "2025", "2026", "2025"],
    ["Net revenue from services rendered", "21", "6,233,239", "5,534,897", "2,332,232", "2,090,465"],
    ["Total revenue and income", "14,681,330", "13,460,661", "5,157,533", "4,660,687"],
    ["Net income for the period", "4,083,759", "3,887,588", "1,381,713", "1,330,105"],
    ["Net income attributable to:"],
    ["Owners of the parent company", "4,082,902", "3,874,081", "1,381,425", "1,319,621"],
    ["Non-controlling interest", "857", "13,507", "288", "10,484"],
    ["Total comprehensive income attributable to:"],
    ["Owners of the parent company", "4,230,486", "4,276,551", "1,516,562", "1,397,068"],
    ["Basic earnings per share", "26", "7.9635", "7.3137", "2.7290", "2.5086"],
    ["Diluted earnings per share", "26", "7.8532", "7.2159", "2.6884", "2.4669"],
)
INTERIM_NOTE = table(
    ["Nine months periods ended September 30,", "Three months period ended September 30,"],
    ["2026", "2025", "2026", "2025"],
    ["Net income attributable to owners of the Parent", "4,082,902", "3,874,081", "1,381,425", "1,319,621"],
    ["Basic weighted average number of outstanding shares (i)(iii)", "512,702", "529,700", "506,199", "526,034"],
    ["Basic earnings per share - R$", "7.9635", "7.3137", "2.7290", "2.5086"],
)


def test_interim_statements_take_the_three_months_group_wherever_it_is():
    p = parse_ifrs_statements(statements(INTERIM_HEADING, INTERIM_BALANCE, INTERIM_INCOME, INTERIM_NOTE))
    assert p.period == pd.Period("2026Q3") and p.currency == "BRL" and p.quarterly
    assert p.values["revenue"] == pytest.approx([5157.533, 4660.687])
    assert p.values["profit"] == pytest.approx([1381.425, 1319.621])  # the owners' row under "Net income attributable to:", not the comprehensive income one
    assert p.values["eps"] == [2.729, 2.5086]
    assert p.values["wa_shares"] == pytest.approx([506.199, 526.034])  # thousands -> millions, the three-month column of the note
    assert p.values["equity"] == pytest.approx([26200.077, 23546.701])  # the note number 18 is not a value


def test_a_first_quarter_has_one_group_and_a_header_cut_off_by_a_page_break_is_kept():
    first = table(
        ["Three months period ended March 31,"],
        ["Note", "2026", "2025"],
        ["Total revenue and income", "4,674,340", "4,344,523"],
        ["Net income for the period", "1,310,000", "1,240,000"],
    )
    rest = table(  # the second page of the income statement has no header
        ["Net income attributable to:"],
        ["Owners of the parent company", "1,309,724", "1,235,519"],
        ["Basic earnings per share", "26", "2.5257", "2.3082"],
    )
    balance = table(["Note", "March 31, 2026", "December 31, 2025"], ["Equity attributable to owners of the Parent company", "24,717,475", "23,546,701"])
    p = parse_ifrs_statements(statements("XP Inc. Interim condensed consolidated financial statements at March 31, 2026", balance, first, rest))
    assert p.period == pd.Period("2026Q1") and p.quarterly
    assert p.values["profit"] == pytest.approx([1309.724, 1235.519]) and p.values["eps"] == [2.5257, 2.3082]
    assert "wa_shares" not in p.values  # no note: the caller implies the shares


ANNUAL_INCOME = table(
    ["Note", "2025", "2024", "2023"],
    ["Total revenue and income", "18,398,597", "17,030,861", "14,860,096"],
    ["Net income attributable to:"],
    ["Owners of the Parent company", "5,169,787", "4,513,409", "3,898,702"],
    ["Non-controlling interest", "(494", "1,431", "689"],
    ["Basic earnings per share", "33", "9.8039", "8.3324", "7.2220"],
)
ANNUAL_BALANCE = table(
    ["Liabilities and equity", "Note", "2025", "2024"],
    ["Equity attributable to owners of the Parent company", "23,546,701", "20,043,557"],
)
ANNUAL_NOTE = table(
    ["2025", "2024", "2023"],
    ["Basic weighted average number of outstanding shares (i) (iii)", "527,318", "541,673", "539,835"],
)


def test_annual_statements_give_the_full_year_and_balance_sheet_columns_that_are_only_years():
    heading = "XP Inc. Consolidated financial statements at December 31, 2025 and independent auditor's report In thousands of Brazilian Reais"
    p = parse_ifrs_statements(statements(heading, ANNUAL_BALANCE, ANNUAL_INCOME, ANNUAL_NOTE))
    assert p.period == pd.Period("2025Q4") and not p.quarterly  # cumulative: Q4 = full year minus nine months, worked out by the caller
    assert p.values["revenue"] == pytest.approx([18398.597, 17030.861]) and p.values["eps"] == [9.8039, 8.3324]
    assert p.values["wa_shares"] == pytest.approx([527.318, 541.673]) and p.values["equity"] == pytest.approx([23546.701, 20043.557])


def test_an_eps_with_a_decimal_comma_is_read_as_a_decimal():
    note = table(
        ["Nine months period ended September 30,", "Three months period ended September 30,"],
        ["2021", "2020", "2021", "2020"],
        ["Basic weighted average number of outstanding shares (i) (iii)", "559,059", "551,800", "559,059", "551,800"],
        ["Basic earnings per share - R$", "4,6506", "2,6742", "1,6736", "0,9794"],
    )
    income = INTERIM_INCOME.replace("2.7290", "1.6736").replace("2.5086", "0.9794").replace("7.9635", "4.6506").replace("7.3137", "2.6742")
    income = income.replace("2026", "2021").replace("2025", "2020")
    balance = INTERIM_BALANCE.replace("2026", "2021").replace("2025", "2020")
    p = parse_ifrs_statements(statements(INTERIM_HEADING, balance, income, note))
    assert p.values["eps"] == [1.6736, 0.9794] and p.values["wa_shares"] == pytest.approx([559.059, 551.8])


def test_disagreeing_tables_and_missing_rows_are_errors_not_guesses():
    wrong_note = INTERIM_NOTE.replace("2.7290", "2.7390")
    with pytest.raises(ValueError, match="eps differs"):
        parse_ifrs_statements(statements(INTERIM_HEADING, INTERIM_BALANCE, INTERIM_INCOME, wrong_note))
    with pytest.raises(ValueError, match="balance sheet"):
        parse_ifrs_statements(statements(INTERIM_HEADING, INTERIM_INCOME))
    with pytest.raises(ValueError, match=r"Not found: \['eps'\]"):
        parse_ifrs_statements(statements(INTERIM_HEADING, INTERIM_BALANCE, INTERIM_INCOME.replace("Basic earnings", "Earnings")))
    with pytest.raises(ValueError, match="unit"):
        parse_ifrs_statements(statements(INTERIM_HEADING, INTERIM_BALANCE.replace("In thousands of Brazilian Reais", ""), INTERIM_INCOME, unit=""))


# ---- the saved statements (data/filings/XP, written by the scheduled update) -------------------------------------------------------------

saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved XP statements (the scheduled update saves them)")


def saved_statements():
    index = [e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if e["results"]]
    return [(e, parse_ifrs_statements((FOLDER / e["file"]).read_bytes())) for e in index]


@saved
def test_every_saved_statement_parses_and_no_quarter_is_missing():
    parsed = saved_statements()
    periods = sorted({p.period for _, p in parsed})
    assert periods == list(pd.period_range("2020Q1", periods[-1], freq="Q")) and periods[-1] >= pd.Period("2026Q3")
    assert all(p.quarterly == (p.period.quarter != 4) for _, p in parsed)  # the fourth quarter comes from the annual statements
    assert all("2019-12-31" not in e["title"] and "December 31, 2019" not in e["title"] for e, _ in parsed)  # the page-image statements are not taken


@saved
def test_each_quarter_agrees_with_the_prior_year_column_a_year_later():
    by_period = {}
    for _, p in saved_statements():
        by_period.setdefault(p.period, p)  # the first quarter of 2021 was furnished twice, identical
    for period, p in by_period.items():
        later = by_period.get(period + 4)
        if later is None or later.quarterly != p.quarterly:
            continue
        for key in ("revenue", "profit", "eps"):
            assert later.values[key][1] == p.values[key][0], (period, key)
        if period.quarter == 4:
            assert later.values["equity"][1] == p.values["equity"][0]


@saved
def test_net_profit_over_eps_matches_the_disclosed_shares():
    """Within 0.01% in every quarter, except four that are XP's own: in 2019Q2, 2020Q2 and 2023Q1 the EPS note divides a slightly different net
    income (R$228.1, 540.3 and 795.9 million) than the income statement shows (R$225.0, 538.3 and 795.0 million), and the 2022Q1 EPS of 1.5273 is
    not the note's 854,104 / 558,871 = 1.5283."""
    quirks = {pd.Period("2019Q2"): -0.0134, pd.Period("2020Q2"): -0.0036, pd.Period("2022Q1"): 0.0006, pd.Period("2023Q1"): -0.0012}
    for _, p in saved_statements():
        if not p.quarterly or "wa_shares" not in p.values:
            continue
        for period, profit, eps, shares in zip([p.period, p.period - 4], p.values["profit"], p.values["eps"], p.values["wa_shares"]):
            deviation = profit / eps / shares - 1
            assert deviation == pytest.approx(quirks.get(period, 0.0), abs=2e-4 if period in quirks else 1e-4), period


def test_the_config_takes_the_ifrs_layout_for_us_stocks_only():
    from valuation_monitor.config import parse_config

    stock = {"symbol": "XP", "name": "XP Inc.", "market": "US", "cik": 1787425, "sec_exhibit": "ifrs_statements", "filing_format": "ifrs_statements"}
    parsed = parse_config({"stocks": [stock]}).stocks[0]
    assert (parsed.filing_format, parsed.sec_exhibit, parsed.sec_form) == ("ifrs_statements", "ifrs_statements", "6-K")
    with pytest.raises(ValueError, match="does not match market"):
        parse_config({"stocks": [{**stock, "symbol": "0001.HK", "market": "HK"}]})
