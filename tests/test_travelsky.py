"""TravelSky Technology (0696.HK): the travelsky_statements layout, on text fragments of its IFRS and CASBE announcements, and on the
announcements saved in the repo (data/filings/0696.HK)."""

import json
from functools import cache
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.filings import Pages, build_history, parse_announcement, parse_period
from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.hkex import RESULTS_TITLE_RE
from valuation_monitor.travelsky import IFRS_NOTE, parse_travelsky_statements

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "0696.HK"


@pytest.mark.parametrize(
    "title,period",
    [
        ("ANNOUNCEMENT OF RESULTS FOR THE SIX MONTHS ENDED JUNE 30, 2026", "2026Q2"),
        ("ANNOUNCEMENT OF ANNUAL RESULTS FOR THE FINANCIAL YEAR ENDED DECEMBER 31, 2025", "2025Q4"),
        ("(REVISED) ANNOUNCEMENT OF ANNUAL RESULTS FOR THE FINANCIAL YEAR ENDED DECEMBER 31, 2022", "2022Q4"),
        ("ANNOUNCEMENT OF RESULTS FOR THE SIX MONTHS ENDED JUNE 30, 2019; AND TERMINATION OF SHARE APPRECIATION RIGHTS SCHEME APPROVED IN 2011", "2019Q2"),
        ("ANNOUNCEMENT OF ANNUAL RESULTS FOR THE FINANCIAL YEAR ENDED DECEMBER 31, 2024 AND PROPOSED ADJUSTMENT TO DIVIDEND POLICY", "2024Q4"),
    ],
)
def test_month_first_titles_are_selected_and_dated(title, period):
    assert RESULTS_TITLE_RE.search(title) and "POLL RESULTS" not in title.upper()
    assert parse_period(title) == pd.Period(period, freq="Q")


@pytest.mark.parametrize(
    "title",
    [
        "ANNOUNCEMENT (1) POLL RESULTS OF THE RESOLUTIONS PASSED AT THE ANNUAL GENERAL MEETING (2) DISTRIBUTION OF FINAL DIVIDEND FOR THE YEAR ENDED 31 DECEMBER 2025",
        "CLARIFICATION ANNOUNCEMENT REGARDING 2022 ANNUAL RESULTS ANNOUNCEMENT",
        "ANNOUNCEMENT POLL RESULTS OF THE RESOLUTION PASSED AT THE EXTRAORDINARY GENERAL MEETING",
    ],
)
def test_other_announcements_are_not_selected(title):
    assert not (RESULTS_TITLE_RE.search(title) and "POLL RESULTS" not in title.upper())


def test_the_day_first_spelling_is_still_read_first():
    assert parse_period("INTERIM RESULTS ANNOUNCEMENT FOR THE SIX MONTHS ENDED 30 JUNE 2026 (ENDED DECEMBER 31, 2025)") == pd.Period("2026Q2", freq="Q")


IFRS_INTERIM = [  # an IFRS interim announcement: the balance sheet comes before the income statement, and the table headers lost their unit
    "Total assets 14,195,985 12,729,607 EQUITY Capital and reserves attributable to Owner of the Parent Paid-In capital 2,926,209 2,926,209 "
    "Reserves 6 3,474,468 3,334,380 Retained earnings – Proposed final cash dividend 7 – 389,186 – Others 4,683,238 3,594,814 "
    "11,083,915 10,318,589 Non-controlling interests 195,912 182,039 Total equity 11,279,827 10,500,628",
    "Data network and others 573,582 484,812 Total revenues 2 2,632,854 2,541,491 Operating expenses Business taxes and other surcharges (10,901) (10,450)",
    "Total comprehensive income for the period 1,179,948 1,153,490 Profit attributable to: Owner of the Parent 1,156,369 1,133,242 "
    "Non-controlling interests 24,273 19,818 1,180,642 1,153,060 Total comprehensive income attributable to: Owner of the Parent 1,155,675 1,133,672 "
    "Non-controlling interests 24,273 19,818 1,179,948 1,153,490 Earnings per share for profit attributable to Owner of the Parent "
    "Basic and diluted (RMB) 5 0.40 0.39",
    "Earnings (RMB’000) Earnings for the purpose of calculating the basic and diluted earnings per share 1,156,369 1,133,242 Numbers of shares (’000) "
    "Weighted average number of ordinary shares in issue 2,926,209 2,926,209 Earnings per share (RMB) Basic and diluted 0.40 0.39",
]

CASBE_INTERIM = [
    "CONSOLIDATED INCOME STATEMENT For the six months ended June 30, 2025 (Amounts are expressed in RMB’000 unless otherwise stated) "
    "I. Total operating income 3,894,500 4,041,791 Including: Operating income 5.f 3,894,500 4,041,791 II. Total operating costs 2,427,877 2,535,291 "
    "(II) Classified by ownership 1. Net profit attributable to shareholders of the Company (“–” for net loss) 1,447,651 1,367,221 "
    "2. Net profit attributable to non-controlling interests (“–” for net loss) 1,132 9,594",
    "VI. Other comprehensive income, net of tax 30,964 45,396 Total comprehensive income attributable to shareholders of the Company 1,478,615 1,412,617 "
    "VIII. Earnings per share: (I) Basic earnings per share (RMB/share) 5.j\x81 0.49 0.47 (II) Diluted earnings per share (RMB/share) 5.j\x81 0.49 0.47",
    "CONSOLIDATED BALANCE SHEET (Amounts are expressed in RMB’000 unless otherwise stated) Shareholders’ equity: Share capital 2,926,210 2,926,210 "
    "Other equity instruments – – Capital reserves 1,151,617 1,159,411 Other comprehensive income 129,911 98,948 Retained earnings 14,065,903 13,489,876 "
    "Total equity attributable to shareholders of the Company 23,084,080 22,304,829 Non-controlling interests 592,313 592,156",
    "Numbers of shares (in thousand shares) (Weighted average number of ordinary shares in issue) 2,926,210 2,926,210 Earnings per share (Basic and diluted) 0.49 0.47",
]


def test_ifrs_interim():
    currency, values, notes = parse_travelsky_statements(IFRS_INTERIM)
    assert currency == "CNY"
    assert values == {
        "revenue": pytest.approx([2632.854, 2541.491]),
        "profit": pytest.approx([1156.369, 1133.242]),
        "eps": pytest.approx([0.40, 0.39]),
        "equity": pytest.approx([11083.915, 10318.589]),  # the total before "Non-controlling interests", not the paid-in capital
        "wa_shares": pytest.approx([2926.209, 2926.209]),
    }
    assert set(notes) == {"eps", "revenue", "profit", "equity"} and all(n == IFRS_NOTE for n in notes.values())
    assert "; " not in IFRS_NOTE  # the saved note column joins notes with "; "


def test_ifrs_loss_and_parentheses():
    pages = [
        "RMB’000 Total revenue 3 2,286,551 3,844,269 Operating expenses",
        "Total comprehensive income for the period (303,543) 1,444,723 (Loss)/profit after taxation attributable to Owners of the Company (323,189) 1,422,997 "
        "Non-controlling interests 16,895 19,554 (306,294) 1,442,551 Total comprehensive income attributable to Owners of the Company (320,438) 1,425,169 "
        "(Losses)/earnings per share for profit attributable to owners of the Company Basic and diluted (RMB) 6 (0.11) 0.49",
        "EQUITY Equity attributable to owners of the Company Share capital 2,926,209 2,926,209 Reserves 5,423,138 5,209,049 Retained earnings 9,261,745 10,641,947 "
        "17,611,092 18,777,205 Non-controlling interests",
    ]
    _, values, _ = parse_travelsky_statements(pages)
    assert values["revenue"] == pytest.approx([2286.551, 3844.269])
    assert values["profit"] == pytest.approx([-323.189, 1422.997])
    assert values["eps"] == pytest.approx([-0.11, 0.49])
    assert values["equity"] == pytest.approx([17611.092, 18777.205])
    assert "wa_shares" not in values


def test_casbe_interim():
    currency, values, notes = parse_travelsky_statements(CASBE_INTERIM)
    assert currency == "CNY" and notes == {}
    assert values == {
        "revenue": pytest.approx([3894.5, 4041.791]),
        "profit": pytest.approx([1447.651, 1367.221]),  # not the non-controlling interests nor the total comprehensive income
        "eps": pytest.approx([0.49, 0.47]),
        "equity": pytest.approx([23084.08, 22304.829]),
        "wa_shares": pytest.approx([2926.21, 2926.21]),
    }


@pytest.mark.parametrize(
    "row,expected",
    [
        ("I. Total operating revenue 5.g) 5,485,577 8,121,673", [5485.577, 8121.673]),
        ("I. Total operating income 4,112,124 3,894,500", [4112.124, 3894.5]),
        ("I. Total operating income 5. f) 4,112,124 3,894,500", [4112.124, 3894.5]),
    ],
)
def test_casbe_revenue_note_variants(row, expected):
    pages = [CASBE_INTERIM[0].replace("I. Total operating income 3,894,500 4,041,791", row), *CASBE_INTERIM[1:]]
    assert parse_travelsky_statements(pages)[1]["revenue"] == pytest.approx(expected)


def test_casbe_negative_figures_with_minus_or_en_dash():
    pages = [
        CASBE_INTERIM[0].replace("(“–” for net loss) 1,447,651 1,367,221", "(“-” for net loss) 275,197 -323,189"),
        CASBE_INTERIM[1].replace("5.j\x81 0.49 0.47 (II)", "5.j) 0.09 -0.11 (II)"),
        CASBE_INTERIM[2].replace("23,084,080 22,304,829", "–2,109 22,304,829"),
        CASBE_INTERIM[3],
    ]
    _, values, _ = parse_travelsky_statements(pages)
    assert values["profit"] == pytest.approx([275.197, -323.189])
    assert values["eps"] == pytest.approx([0.09, -0.11])
    assert values["equity"][0] == pytest.approx(-2.109)


def test_casbe_other_equity_instruments_are_an_error():
    pages = [*CASBE_INTERIM[:2], CASBE_INTERIM[2].replace("Other equity instruments – –", "Other equity instruments 1,000,000 –"), CASBE_INTERIM[3]]
    with pytest.raises(ValueError, match="other equity instruments"):
        parse_travelsky_statements(pages)


@pytest.mark.parametrize(
    "drop,message",
    [
        ("1. Net profit attributable to shareholders of the Company", "net profit"),
        ("Total equity attributable to shareholders of the Company", "equity"),
        ("(I) Basic earnings per share", "eps"),
    ],
)
def test_a_missing_row_is_an_error(drop, message):
    pages = [p.replace(drop, "Something else") for p in CASBE_INTERIM]
    with pytest.raises(ValueError, match=message):
        parse_travelsky_statements(pages)


def test_no_income_statement_or_unit_is_an_error():
    with pytest.raises(ValueError, match="income statement"):
        parse_travelsky_statements(["Revenue 1,000 900"])
    with pytest.raises(ValueError, match="thousands"):
        parse_travelsky_statements([p.replace("RMB’000", "RMB") for p in CASBE_INTERIM])


# ---- the announcements saved in the repo ----

saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved TravelSky announcements")


@cache
def announcements():
    index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    filings = [Filing(e["title"], pd.Timestamp(e["published"]), e["url"], (FOLDER / e["file"]).read_bytes()) for e in index]
    return [parse_announcement(f, "travelsky_statements") for f in filings]


@saved
def test_every_saved_announcement_parses_and_agrees_with_its_shares():
    parsed = announcements()
    assert len(parsed) >= 25
    for a in parsed:
        assert a.currency == "CNY" and not a.is_quarterly_table and a.period.quarter in (2, 4)
        profit, eps, shares = a.values["profit"][0], a.values["eps"][0], a.values["wa_shares"][0]
        assert shares == pytest.approx(2926.21, abs=0.01)  # unchanged since 2014: no split, bonus issue or buyback
        assert abs(profit / eps / shares - 1) <= abs(0.005 / eps) + 1e-9, a.url  # within the rounding of the EPS to the fen


@saved
def test_saved_ifrs_announcements_carry_the_note_until_the_2020_interim_results():
    for a in announcements():
        ifrs = a.period <= pd.Period("2020Q2", freq="Q")
        assert (a.series_notes == {s: IFRS_NOTE for s in ("eps", "revenue", "profit", "equity")}) is ifrs, a.url
        if not ifrs:
            assert a.series_notes == {}


@saved
def test_saved_figures():
    by_period = {(a.period, a.published.date()): a.values for a in announcements()}
    first_casbe = by_period[(pd.Period("2020Q4", freq="Q"), pd.Timestamp("2021-03-31").date())]
    assert first_casbe["revenue"] == pytest.approx([5485.577, 8121.673]) and first_casbe["profit"] == pytest.approx([362.8, 2526.006])
    assert first_casbe["eps"] == pytest.approx([0.12, 0.87])
    latest = by_period[(pd.Period("2026Q2", freq="Q"), pd.Timestamp("2026-08-24").date())]
    expected = {"revenue": [4112.124, 3894.5], "profit": [1543.01, 1447.651], "eps": [0.53, 0.49], "equity": [25030.046, 23983.764], "wa_shares": [2926.21, 2926.21]}
    assert latest == {k: pytest.approx(v) for k, v in expected.items()}
    revised = [v for (p, _), v in by_period.items() if p == pd.Period("2022Q4", freq="Q")]
    assert len(revised) == 2 and revised[0] == revised[1]  # the revised 2022 announcement corrects a typo in total net profit only


@saved
def test_saved_history():
    history = build_history(announcements())
    assert history.step == 2  # half-yearly
    ttm = history.ttm_frame().set_index("report_date")
    last = ttm.loc[pd.Timestamp("2026-06-30")]
    assert last["eps_ttm"] == pytest.approx(0.84)  # 2025H2 0.80 - 0.49 + 2026H1 0.53
    assert last["revenue_ttm"] == pytest.approx(8983.464, abs=0.05)  # 8,765.840 - 3,894.500 + 4,112.124, saved to one decimal
    assert last["equity"] == pytest.approx(25030.046, abs=0.05) and last["shares"] == pytest.approx(2926.21)
    h2_2020 = ttm.loc[pd.Timestamp("2020-12-31")]
    assert h2_2020["eps_ttm"] == pytest.approx(0.12)  # the CASBE full year: 2020H1 (IFRS) -0.11 + 2020H2 0.23
    # the only notices: 2019 under IFRS against its CASBE comparatives, and the slightly restated 2022 figures
    notices = history.discrepancies()
    assert notices and all(("2019H2" in n or "2022H2" in n) for n in notices), notices
