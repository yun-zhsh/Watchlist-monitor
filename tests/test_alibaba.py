"""Alibaba Group (BABA) press releases in the `press_release` layout.

The snippet tests run on small tables shaped like the real ones; the saved-data tests run on the original releases in
data/filings/BABA/ and are skipped until the scheduled workflow has saved them.
"""

import json
from functools import cache
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.press_release import parse_press_release, shares_per_ads, to_number

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "BABA"
saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved Alibaba filings")


def table(*rows: list[str]) -> str:
    return "<table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"


def release(*tables: str) -> bytes:
    return ("<html><body><p>Alibaba Group Announces June Quarter 2025 Results</p>" + "".join(tables) + "</body></html>").encode()


INCOME_2025 = table(
    ["", "Three months ended June 30,"],
    ["", "2024", "2025"],
    ["", "RMB", "RMB", "US$"],
    ["", "(in millions, except per share data)"],
    ["Revenue", "243,236", "247,652", "34,571"],
    ["Net income", "24,022", "42,382", "5,916"],
    ["Net loss (income) attributable to noncontrolling interests", "368", "(1,733", "(242"],
    ["Net income attributable to Alibaba Group Holding Limited", "24,390", "40,649", "5,674"],
    ["(Accretion) Reversal of accretion of mezzanine equity", "(121", "2,467", "345"],
    ["Net income attributable to ordinary shareholders", "24,269", "43,116", "6,019"],
    ["Earnings per share attributable to ordinary shareholders (1)"],
    ["Basic", "1.26", "2.32", "0.32"],
    ["Diluted", "1.24", "2.25", "0.31"],
    ["Earnings per ADS attributable to ordinary shareholders (1)"],
    ["Basic", "10.04", "18.57", "2.59"],
    ["Diluted", "9.89", "17.98", "2.51"],
    ["Weighted average number of shares used in calculating earnings per ordinary share (million shares) (1)"],
    ["Basic", "19,329", "18,570"],
    ["Diluted", "19,595", "19,142"],
)
BALANCE_2025 = table(
    ["", "As of March 31,", "As of June 30,"],
    ["", "2025", "2025"],
    ["", "RMB", "RMB", "US$"],
    ["", "(in millions)"],
    ["Total assets", "1,804,227", "1,847,085", "257,843"],
    ["Total liabilities", "714,121", "753,766", "105,222"],
    ["Total shareholders' equity", "1,009,858", "1,013,672", "141,503"],
    ["Noncontrolling interests", "68,535", "69,726", "9,734"],
    ["Total equity", "1,078,393", "1,083,398", "151,237"],
)


def test_years_only_header_per_ads_eps_and_eight_shares_per_ads():
    p = parse_press_release(release(INCOME_2025, BALANCE_2025))
    assert p.period == pd.Period("2025Q2")
    assert p.currency == "CNY"
    assert p.values["revenue"] == [247652.0, 243236.0]
    assert p.values["profit"] == [43116.0, 24269.0]  # attributable to ordinary shareholders, not to the company before the accretion
    assert p.values["eps"] == [18.57, 10.04]  # per ADS
    assert p.values["wa_shares"] == [18570 / 8, 19329 / 8]  # millions of shares -> millions of ADS (1 ADS = 8 shares)
    assert "non_ifrs_eps" not in p.values  # Alibaba discloses only a diluted Non-GAAP EPS
    # the comparison balance is the fiscal year end (March 31), not the previous December: only this period's equity is kept;
    # "Total shareholders' equity" is the company's share because "Total equity" follows the noncontrolling interests
    assert p.values["equity"] == [1013672.0]


def test_before_the_2019_subdivision_the_per_share_eps_is_the_per_ads_eps():
    income = table(
        ["", "Three months ended June 30,", "Year ended June 30,"],
        ["", "2014", "2015", "2014", "2015"],
        ["", "RMB", "RMB", "US$", "RMB", "RMB", "US$"],
        ["", "(in millions, except per share data)", "(in millions, except per share data)"],
        ["Revenue", "15,771", "20,245", "3,265", "52,504", "76,204", "12,293"],
        ["Net income attributable to ordinary shareholders", "12,344", "30,8 43", "4,975", "1", "2", "3"],
        ["Earnings per share attributable to ordinary shareholders"],
        ["Basic", "5.62", "12.4 5", "2.01", "1", "2", "3"],
        ["Diluted", "5.27", "11.83", "1.91", "1", "2", "3"],
        ["Weighted average number of share used in calculating net income per ordinary share"],
        ["Basic", "2,198", "2,477", "1", "2"],
    )
    balance = table(
        ["", "As of March 31,", "As of June 30,"],
        ["", "2015", "2015"],
        ["", "RMB", "RMB", "US$"],
        ["", "(in millions)"],
        ["Total liabilities", "97,363", "110,000", "17,000"],
        ["Total Alibaba Group Holding Limited shareholders' equity", "145,439", "182,874", "29,496"],
        ["Noncontrolling interests", "11,974", "12,000", "1,900"],
        ["Total equity", "157,413", "194,874", "31,396"],
    )
    p = parse_press_release(release(income, balance))
    assert p.period == pd.Period("2015Q2")
    assert p.values["profit"] == [30843.0, 12344.0]  # "30,8 43": a number broken by a stray space
    assert p.values["eps"] == [12.45, 5.62]
    assert p.values["wa_shares"] == [2477.0, 2198.0]  # one ADS was one share
    assert p.values["equity"] == [182874.0]


def test_loss_earnings_per_ads_label_and_december_comparison_keeps_both_balances():
    income = table(
        ["", "Three months ended December 31,"],
        ["", "2022", "2023"],
        ["", "RMB", "RMB", "US$"],
        ["", "(in millions, except per share data)"],
        ["Revenue", "247,756", "260,348", "36,669"],
        ["Net (loss) income attributable to ordinary shareholders", "(20,561", "27,706", "3,797"],
        ["(Loss) Earnings per share attributable to ordinary shareholders (1)"],
        ["Basic", "(0.97", "1.36", "0.19"],
        ["(Loss) Earnings per ADS attributable to ordinary shareholders (1)"],
        ["Basic", "(7.77", "10.90", "1.49"],
        ["Weighted average number of shares used in calculating earnings per ordinary share (million shares) (1)"],
        ["Basic", "21,164", "20,335"],
    )
    balance = table(
        ["", "As of December 31,", "As of December 31,"],
        ["", "2022", "2023"],
        ["", "RMB", "RMB", "US$"],
        ["", "(in millions)"],
        ["Total liabilities", "1", "2", "3"],
        ["Total shareholders' equity", "975,954", "1,010,590", "142,340"],
        ["Noncontrolling interests", "1", "2", "3"],
        ["Total equity", "2", "3", "4"],
    )
    p = parse_press_release(release(income, balance))
    assert p.period == pd.Period("2023Q4")
    assert p.values["eps"] == [10.90, -7.77]
    assert p.values["profit"] == [27706.0, -20561.0]
    assert p.values["wa_shares"] == [20335 / 8, 21164 / 8]
    assert p.values["equity"] == [1010590.0, 975954.0]


def test_spaced_numbers():
    assert to_number("2,86 9") == 2869.0
    assert to_number("(6, 133") == -6133.0
    assert to_number("1.6 7") == 1.67
    assert to_number("( 2") == -2.0
    assert to_number("12 and 3") is None
    assert to_number("YoY %") is None


def test_shares_per_ads():
    assert shares_per_ads([18.57, 10.04], [2.32, 1.26]) == 8
    assert shares_per_ads([4.83, 3.72], [2.42, 1.86]) == 2  # JD
    assert shares_per_ads([12.45], None) == 1
    with pytest.raises(ValueError, match="whole number"):
        shares_per_ads([18.57, 10.04], [3.32, 1.26])


# ---- the saved original releases ----


@cache
def index() -> list[dict]:
    return [e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if e["results"]]


@cache
def parsed(file: str):
    return parse_press_release((FOLDER / file).read_bytes())


def by_period(period: str):
    for e in index():
        p = parsed(e["file"])
        if p.period == pd.Period(period):
            return p
    raise KeyError(period)


@saved
def test_every_saved_release_parses_and_profit_over_eps_matches_the_ads():
    periods = []
    for e in index():
        p = parsed(e["file"])
        periods.append(p.period)
        assert {"revenue", "profit", "eps", "wa_shares", "equity"} <= p.values.keys(), e["file"]
        for profit, eps, ads in zip(p.values["profit"], p.values["eps"], p.values["wa_shares"]):
            assert abs(profit / eps / ads - 1) < 0.005, (e["file"], profit, eps, ads)  # basic EPS and the disclosed shares are on the same ADS basis
    assert periods == sorted(periods)
    assert len(set(periods)) == len(periods)
    assert all(b == a + 1 for a, b in zip(periods, periods[1:]))  # no quarter is missing (the Hong Kong-style announcements included)


@saved
def test_prior_year_eps_matches_the_earlier_release():
    """The prior-year column is on the same per-ADS basis as the release of that quarter (also across the 2019 subdivision)."""
    own = {parsed(e["file"]).period: parsed(e["file"]).values["eps"][0] for e in index()}
    differs = []
    for e in index():
        p = parsed(e["file"])
        earlier = own.get(p.period - 4)
        if earlier is not None and abs(p.values["eps"][1] - earlier) > 0.011:
            differs.append(str(p.period - 4))
    assert differs == ["2021Q4"]  # restated in the December quarter 2022 release ("Revised attribution to noncontrolling interests")


@saved
def test_values_of_selected_quarters():
    p = by_period("2015Q2")  # per-share figures (1 ADS = 1 share), numbers broken by stray spaces in the original
    assert p.values["profit"][0] == 30843.0 and p.values["eps"][0] == 12.45 and p.values["wa_shares"][0] == 2477.0
    p = by_period("2019Q3")  # after the 1-to-8 subdivision: per ADS, shares / 8
    assert p.values["eps"] == [27.9, 7.75] and p.values["wa_shares"] == [2600.0, 20669 / 8]
    p = by_period("2025Q1")  # the Hong Kong-style annual results announcement
    assert p.values["revenue"] == [236454.0, 221874.0] and p.values["eps"] == [5.36, 1.32] and p.values["equity"] == [1009858.0]
