"""PDD Holdings (PDD; Pinduoduo Inc. until February 2023) press releases in the `press_release` layout.

The snippet tests run on small tables shaped like the real ones (figures from the releases); each exercises one of the rules appended for
PDD. The saved-data tests run on the original releases in data/filings/PDD/ and are skipped until the scheduled workflow has saved them.
"""

import json
from functools import cache
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor import press_release
from valuation_monitor.press_release import parse_press_release

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "PDD"
saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved PDD filings (the scheduled update saves them)")

THOUSANDS = "(Amounts in thousands of RMB and US$, except for per share data)"
THOUSANDS_RAW_SHARES = "(Amounts in thousands of RMB and US$, except for number of shares and per share data)"


def table(*rows: list[str]) -> str:
    return "<table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"


def release(*parts: str) -> bytes:
    return ("<html><body><p>Pinduoduo Announces Second Quarter 2019 Unaudited Financial Results</p>" + "".join(parts) + "</body></html>").encode()


def note(text: str) -> str:
    return f"<p>{text}</p>"


QUARTER_HEADER = [
    ["", "For the three months ended June 30,", "For the six months ended June 30,"],
    ["", "2018", "2019", "2018", "2019"],
    ["", "RMB", "RMB", "US$", "RMB", "RMB", "US$"],
    ["", "(Unaudited)", "(Unaudited)", "(Unaudited)", "(Unaudited)", "(Unaudited)", "(Unaudited)"],
]
# the June quarter 2019 release
BALANCE_2019Q2 = table(
    ["", "As of"],
    ["", "December 31, 2018", "June 30, 2019"],  # one date per RMB column; the US dollar column has none
    ["", "RMB", "RMB", "US$"],
    ["", "(Unaudited)"],
    ["Total Liabilities", "24,359,469", "29,253,965", "4,261,320"],
    ["Shareholders' equity"],
    ["Total shareholders' equity", "18,822,594", "25,106,885", "3,657,230"],
    ["Total liabilities and shareholders' equity", "43,182,063", "54,360,850", "7,918,550"],
)
REVENUE_2019Q2 = table(
    *QUARTER_HEADER,
    ["Revenues"],
    ["Online marketplace services", "2,709,046", "7,290,008", "1,061,909", "4,093,650", "11,835,212", "1,723,993"],
    ["Total Revenues", "2,709,046", "7,290,008", "1,061,909", "4,093,650", "11,835,212", "1,723,993"],
)
EPS_2019Q2 = table(
    *QUARTER_HEADER,
    ["Net loss", "(6,493,888", "(1,003,250", "(146,139", "(6,694,909", "(2,880,958", "(419,658"],
    ["Net loss attributable to ordinary shareholders", "(6,493,888", "(1,003,250", "(146,139", "(6,775,405", "(2,880,958", "(419,658"],
    ["Loss per share for Class A and Class B ordinary shares:"],
    ["-Basic", "(3.35", "(0.22", "(0.03", "(3.66", "(0.63", "(0.09"],
    ["-Diluted", "(3.35", "(0.22", "(0.03", "(3.66", "(0.63", "(0.09"],
    ["Loss per ADS (4 ordinary shares equals 1 ADS ):"],
    ["-Basic", "(13.40", "(0.88", "(0.12", "(14.64", "(2.52", "(0.36"],
    ["-Diluted", "(13.40", "(0.88", "(0.12", "(14.64", "(2.52", "(0.36"],
    ["Weighted average number of Class A and Class B ordinary shares outstanding (in thousands):"],
    ["-Basic", "1,940,537", "4,649,429", "4,649,429", "1,850,155", "4,604,472", "4,604,472"],
    ["-Diluted", "1,940,537", "4,649,429", "4,649,429", "1,850,155", "4,604,472", "4,604,472"],
)


def non_gaap(basic: tuple[str, str], diluted: tuple[str, str]) -> str:
    """The reconciliation table: Non-GAAP EPS rows with figures of their own (not a heading over "Basic")."""
    return table(
        *QUARTER_HEADER,
        ["Net loss attributable to ordinary shareholders", "(6,493,888", "(1,003,250", "(146,139", "(6,775,405", "(2,880,958", "(419,658"],
        ["Add: Share-based compensation", "5,820,490", "591,975", "86,231", "5,834,100", "1,090,607", "158,865"],
        ["Non-GAAP net loss attributable to ordinary shareholders", "(673,398", "(411,275", "(59,908", "(941,305", "(1,790,351", "(260,793"],
        ["Non-GAAP basic loss per ADS", basic[0], basic[1], "(0.04", "(2.04", "(1.56", "(0.24"],
        ["Non-GAAP diluted loss per ADS", diluted[0], diluted[1], "(0.04", "(2.04", "(1.56", "(0.24"],
    )


def release_2019q2(non_gaap_table: str = "") -> bytes:
    return release(note(THOUSANDS), BALANCE_2019Q2, note(THOUSANDS), REVENUE_2019Q2, note(THOUSANDS), EPS_2019Q2, note(THOUSANDS), non_gaap_table)


def test_one_date_per_rmb_column_total_revenues_dash_basic_and_four_shares_per_ads():
    p = parse_press_release(release_2019q2(non_gaap(("(1.40", "(0.36"), ("(1.40", "(0.36"))), "diluted")
    assert p.period == pd.Period("2019Q2") and p.currency == "CNY"
    assert p.values["revenue"] == pytest.approx([7290.008, 2709.046])
    assert p.values["profit"] == pytest.approx([-1003.25, -6493.888])
    assert p.values["eps"] == [-0.88, -13.40]  # per ADS: 4 x the basic EPS per share (-0.22)
    assert p.values["wa_shares"] == pytest.approx([4649.429 / 4, 1940.537 / 4])  # millions of ADS
    assert p.values["equity"] == pytest.approx([25106.885, 18822.594])  # 2019-06-30, 2018-12-31
    assert p.values["non_ifrs_eps"] == [-0.36, -1.40]


def test_diluted_basis_takes_the_diluted_non_gaap_eps_although_a_basic_one_is_given():
    # the September quarter 2020 release: basic 0.39, diluted 0.33 per ADS (the only quarter where they differ before 2021)
    html = release_2019q2(non_gaap(("(1.40", "0.39"), ("(1.40", "0.33")))
    assert parse_press_release(html, "diluted").values["non_ifrs_eps"] == [0.33, -1.40]


def test_diluted_basis_never_takes_a_basic_non_gaap_eps():
    """A release with a basic Non-GAAP EPS section and no diluted one: on the diluted basis it has no Non-GAAP EPS (JD's basic basis keeps it)."""
    basic_section = table(
        *QUARTER_HEADER,
        ["Non-GAAP net loss per ADS"],
        ["Basic", "(1.40", "(0.36", "(0.04", "(2.04", "(1.56", "(0.24"],
    )
    html = release_2019q2(basic_section)
    assert parse_press_release(html).values["non_ifrs_eps"] == [-0.36, -1.40]
    assert parse_press_release(html, "basic").values["non_ifrs_eps"] == [-0.36, -1.40]
    assert "non_ifrs_eps" not in parse_press_release(html, "diluted").values


def test_2018_single_shares_deficits_and_a_date_without_a_space():
    """The June quarter 2018 release (before the July 2018 IPO): share counts in single shares, as the unit note says; a shareholders' deficit."""
    balance = table(
        ["", "As of"],
        ["", "December 31,2017", "June 30, 2018"],
        ["", "RMB", "RMB", "US$"],
        ["", "(Audited)", "(Unaudited)"],
        ["Total liabilities", "13,991,917", "24,833,651", "3,752,951"],
        ["Mezzanine equity", "314,511", "—", "—"],
        ["Total shareholders' deficits", "(991,958", "(1,560,147", "(235,776"],
    )
    income = table(
        ["", "For the three months ended June 30,", "For the six months ended June 30,"],
        ["", "2017", "2018", "2017", "2018"],
        ["", "RMB", "RMB", "US$", "RMB", "RMB", "US$"],
        ["Total Revenues", "1,000", "2,709,046", "409,393", "1,500", "4,093,650", "618,656"],
        ["Net loss attributable to ordinary shareholders", "(106,679", "(6,493,888", "(981,368", "(200,000", "(6,775,405", "(1,023,914"],
        ["Earnings per share for Class A and Class B ordinary shares:"],
        ["-Basic", "(0.06", "(3.35", "(0.51", "(0.11", "(3.66", "(0.55"],
        ["Earnings per ADS (4 Class A ordinary share equals 1 ADSs ):"],
        ["-Basic", "(0.24", "(13.40", "(2.03", "(0.44", "(14.64", "(2.21"],
        ["Weighted average number of Class A and Class B ordinary shares outstanding:"],
        ["-Basic", "1,758,769,820", "1,940,536,606", "1,940,536,606", "1,758,769,820", "1,850,155,230", "1,850,155,230"],
    )
    p = parse_press_release(release(note(THOUSANDS_RAW_SHARES), balance, note(THOUSANDS_RAW_SHARES), income), "diluted")
    assert p.period == pd.Period("2018Q2")
    assert p.values["equity"] == pytest.approx([-1560.147, -991.958])
    assert p.values["wa_shares"] == pytest.approx([1940.536606 / 4, 1758.769820 / 4])
    assert p.values["eps"] == [-13.40, -0.24]


def test_years_ended_unaudited_currency_cells_slash_headings_and_a_split_date():
    """The December quarter: "For the years ended December 31,"; currency cells "RMB (Unaudited)"; "(Loss)/ earnings per ADS";
    the balance sheet's first date over two rows ("December" over "31, 2019") and "December 31 , 2021"."""
    header = [
        ["", "For the three months ended December 31,", "For the years ended December 31,"],
        ["", "2020", "2021", "2020", "2021"],
        ["", "RMB (Unaudited)", "RMB (Unaudited)", "US$ (Unaudited)", "RMB (Unaudited)", "RMB (Unaudited)", "US$ (Unaudited)"],
    ]
    balance = table(
        ["", "As of"],
        ["", "December"],
        ["", "31, 2020", "December 31 , 2021"],
        ["", "RMB", "RMB", "US$"],
        ["Total Liabilities", "98,732,726", "106,094,971", "16,648,722"],
        ["Total Shareholders' Equity", "60,175,888", "75,114,547", "11,787,234"],
    )
    income = table(
        *header,
        ["Revenues", "26,547,709", "27,230,862", "4,273,163", "59,491,925", "93,949,939", "14,742,846"],
        ["Net (loss)/ income attributable to ordinary shareholders", "(1,376,356", "6,619,546", "1,038,750", "(7,179,742", "7,768,670", "1,219,075"],
        ["(Loss)/ earnings per ordinary share:"],
        ["-Basic", "(0.28", "1.32", "0.21", "(1.52", "1.55", "0.24"],
        ["(Loss)/ earnings per ADS (4 ordinary shares equals 1 ADS):"],
        ["-Basic", "(1.13", "5.26", "0.83", "(6.08", "6.18", "0.97"],
        ["Weighted average number of outstanding ordinary shares (in thousands):"],
        ["-Basic", "4,854,597", "5,031,543", "5,031,543", "4,719,553", "5,021,386", "5,021,386"],
    )
    reconciliation = table(
        *header,
        ["Non-GAAP diluted (loss)/ earnings per ADS", "(0.15", "5.88", "0.92", "(5.40", "9.06", "1.42"],
    )
    p = parse_press_release(release(note(THOUSANDS), balance, note(THOUSANDS), income, note(THOUSANDS), reconciliation), "diluted")
    assert p.period == pd.Period("2021Q4")
    assert p.values["revenue"] == pytest.approx([27230.862, 26547.709])
    assert p.values["profit"] == pytest.approx([6619.546, -1376.356])
    assert p.values["eps"] == [5.26, -1.13]
    assert p.values["equity"] == pytest.approx([75114.547, 60175.888])
    assert p.values["non_ifrs_eps"] == [5.88, -0.15]


def test_a_unit_note_broken_over_two_strings_and_shares_in_millions():
    """2026: amounts in millions; the balance sheet's note is two strings, "(Amounts in" and "millions of Renminbi ..."."""
    header = [
        ["", "For the three months ended March 31,"],
        ["", "2025", "2026"],
        ["", "RMB", "RMB", "US$"],
    ]
    balance = table(
        ["", "As of"],
        ["", "December 31, 2025", "March 31, 2026"],
        ["", "RMB", "RMB (Unaudited)", "US$ (Unaudited)"],
        ["Total Liabilities", "216,660", "214,277", "31,064"],
        ["Total Shareholders' Equity", "413,385", "423,427", "61,384"],
    )
    income = table(
        *header,
        ["Revenues", "95,672", "106,229", "15,400"],
        ["Net income attributable to ordinary shareholders", "14,742", "12,547", "1,819"],
        ["Earnings per ordinary share:"],
        ["-Basic", "2.65", "2.23", "0.32"],
        ["Earnings per ADS (4 ordinary shares equals 1 ADS):"],
        ["-Basic", "10.59", "8.94", "1.30"],
        ["Weighted-average number of ordinary shares outstanding (in millions):"],
        ["-Basic", "5,568", "5,616", "5,616"],
        ["Non-GAAP diluted earnings per ADS", "11.41", "9.51", "1.38"],
    )
    html = release("<p>(Amounts in<span> millions of Renminbi (RMB) and U.S. dollars (US$))</span></p>", balance,
                   note("(Amounts in millions of RMB and US$, except for per share data)"), income)
    p = parse_press_release(html, "diluted")
    assert p.period == pd.Period("2026Q1")
    assert p.values["equity"] == pytest.approx([423427.0, 413385.0])
    assert p.values["wa_shares"] == pytest.approx([5616 / 4, 5568 / 4])
    assert p.values["non_ifrs_eps"] == [9.51, 11.41]


def test_the_rules_appended_for_pdd_never_replace_what_the_jd_rules_find():
    """A table with both a "Basic" row (JD) and a "-Basic" row (PDD) under one heading takes the "Basic" row."""
    rows = [
        *QUARTER_HEADER,
        ["Net loss per ADS"],
        ["-Basic", "(9.99", "(9.99", "(9.99", "(9.99", "(9.99", "(9.99"],
        ["Basic", "(13.40", "(0.88", "(0.12", "(14.64", "(2.52", "(0.36"],
    ]
    header = press_release.parse_header([[c for c in r if c] for r in rows])
    found = press_release.parse_flow_rows([[c for c in r if c] for r in rows], header, 0.001)
    assert found["eps"] == (pd.Period("2019Q2"), [-0.88, -13.40])


# ---- the saved original releases ----


@cache
def releases() -> dict[pd.Period, tuple[str, object]]:
    entries = [e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if e.get("results")]
    out = {}
    for e in entries:
        p = parse_press_release((FOLDER / e["file"]).read_bytes(), "diluted")
        out[p.period] = (e["published"], p)
    return out


@saved
def test_every_saved_release_parses_one_per_quarter_since_the_ipo():
    periods = sorted(releases())
    assert periods[0] == pd.Period("2018Q2")
    assert periods == list(pd.period_range(periods[0], periods[-1], freq="Q"))
    assert len(periods) >= 34  # 2018Q2 to 2026Q3 when this was written


@saved
def test_saved_releases_net_income_over_eps_agrees_with_the_disclosed_ads():
    """Within 2%: the per-ADS EPS is 4 x the per-share EPS rounded to the cent (2019Q2: -0.22 x 4 = -0.88, unrounded -0.86)."""
    for period, (_, p) in releases().items():
        v = p.values
        implied = v["profit"][0] / v["eps"][0]
        assert abs(implied / v["wa_shares"][0] - 1) < 0.02, (period, implied, v["wa_shares"][0])


@saved
def test_saved_releases_prior_year_columns_are_the_first_published_figures():
    """No restatement of revenue, net income, EPS or the diluted Non-GAAP EPS (also across the reconciling-item changes of 2019-2026)."""
    found = releases()
    for period, (_, p) in found.items():
        earlier = found.get(period - 4)
        if earlier is None:
            continue
        for key in ("revenue", "profit", "eps", "non_ifrs_eps"):
            first, shown = earlier[1].values[key][0], p.values[key][1]
            assert abs(first - shown) <= (0.006 if key in ("eps", "non_ifrs_eps") else max(1.0, abs(first) * 0.001)), (period, key, first, shown)


@saved
def test_saved_release_figures():
    found = releases()
    q2_2018 = found[pd.Period("2018Q2")][1].values
    assert q2_2018["eps"][0] == -13.40 and q2_2018["equity"][0] == -1560.147  # before the IPO: a shareholders' deficit
    assert q2_2018["wa_shares"][0] == pytest.approx(485.134, abs=0.001)  # single shares in the release
    assert found[pd.Period("2020Q3")][1].values["non_ifrs_eps"][0] == 0.33  # diluted; the basic one was 0.39
    q3_2026 = found[pd.Period("2026Q3")][1].values
    assert q3_2026["revenue"][0] == 108341 and q3_2026["profit"][0] == 29541 and q3_2026["eps"][0] == 20.98
    assert q3_2026["non_ifrs_eps"][0] == 21.04 and q3_2026["equity"][0] == 475661
