"""The adjusted EPS of XP's earnings press release, read next to its IFRS statements (release_eps.py), and how a supplementary document joins the history."""

import json
import os
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.config import parse_config
from valuation_monitor.filings import (
    SUPPLEMENT_PARSER_VERSION,
    Announcement,
    announcements_from_frame,
    announcements_to_frame,
    build_history,
    parse_announcement,
)
from valuation_monitor.market_calendar import get_market
from valuation_monitor.providers.base import Filing
from valuation_monitor.release_eps import is_results_release_title, parse_adjusted_eps_release

ROOT = Path(__file__).resolve().parent.parent
FOLDER = Path(os.environ.get("VALUATION_DATA_DIR", ROOT / "data")) / "filings" / "XP"  # another data directory can be checked with VALUATION_DATA_DIR
US = get_market("US")


def release(title, *paragraphs):
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return f"<html><body><p>EXHIBIT 99.1</p><p>{title}</p>{body}</body></html>".encode()


FOURTH_QUARTER = release(
    "XP Inc. Reports Fourth Quarter 2024 Results",
    "Adjusted Net Income and Adjusted EPS 1 In 4Q24, Adjusted Net Income reached a record R$1.2 billion , showing a 2% QoQ and a 16% rise YoY.",
    "Adjusted Basic EPS was R$2.25, reflecting a 2% QoQ growth and an 18% YoY increase. Adjusted diluted EPS was R$2.23 for the quarter, up 19% QoQ.",
    "For the full year 2024, adjusted net income also hit a record high of R$4.5 billion . Adjusted Basic EPS was R$8.39, up 16% from the previous year.",
)


def test_the_adjusted_basic_eps_of_the_quarter_is_taken_not_the_full_year_or_the_diluted_one():
    parsed = parse_adjusted_eps_release(FOURTH_QUARTER)
    assert parsed.period == pd.Period("2024Q4") and parsed.currency == "BRL"
    assert parsed.values == {"non_ifrs_eps": [2.25]}


def test_a_release_without_an_adjusted_eps_gives_nothing_not_the_reported_eps():
    no_adjustments = release(
        "Earnings Release August 18th, 2025 XP Inc. Reports Second Quarter 2025 Results",
        "Net Income and EPS In 2Q25, Net Income reached a record of R$1.3 billion , showing a 7% QoQ and a 18% rise YoY.",
        "Basic EPS was R$2.50, reflecting a 8% QoQ growth and an 22% YoY increase. Diluted EPS was R$2.46 for the quarter.",
    )
    only_net_income = release("Exhibit 99.1 XP Inc. Reports 2Q20 Financial Results", "Adjusted Net Income¹ (in R$ mn) 565 228 148% 415 36%")
    diluted_only = release("XP Inc. Reports First Quarter 2026 Results", "In 1Q26, Adjusted Net Income reached R$1.3 billion . Adjusted Diluted EPS was R$2.49.")
    assert parse_adjusted_eps_release(no_adjustments) == ("BRL", {}, pd.Period("2025Q2"), True)
    assert parse_adjusted_eps_release(only_net_income).values == {} and parse_adjusted_eps_release(only_net_income).period == pd.Period("2020Q2")
    assert parse_adjusted_eps_release(diluted_only).values == {}  # XP's PE Non-GAAP is basic: a diluted figure is not taken in its place


def test_the_quarter_comes_from_the_title_or_the_first_quarter_label_and_must_agree_with_the_sentence():
    no_year = release("XP Inc. Reports Second Quarter Financial Results", "2Q22 Earnings Release", "Total revenue was R$3.0 billion.")
    assert parse_adjusted_eps_release(no_year).period == pd.Period("2022Q2")
    full_year = release("XP Inc. Reports Fourth Quarter and Full-Year 2022 Financial Results", "4Q22 Highlights")
    assert parse_adjusted_eps_release(full_year).period == pd.Period("2022Q4")
    wrong = release("XP Inc. Reports Third Quarter 2026 Results", "In 2Q26, Adjusted Net Income reached R$1.4 billion . Adjusted Basic EPS was R$2.71.")
    with pytest.raises(ValueError, match="states the Adjusted Basic EPS of 2026Q2"):
        parse_adjusted_eps_release(wrong)
    with pytest.raises(ValueError, match="reporting quarter"):
        parse_adjusted_eps_release(release("XP INC. ANNOUNCES RESULTS", "XP will report its results on May 20."))


def test_only_titles_reporting_a_quarter_are_results_releases():
    assert is_results_release_title("Exhibit 99.1 XP Inc. Reports Third Quarter 2026 Results")
    assert is_results_release_title("Exhibit 99.1 XP Inc. Reports 4Q19 and 2019 FY Financial Results")
    assert not is_results_release_title("EXHIBIT 99.2 XP INC. ANNOUNCES PRELIMINARY ESTIMATED 2Q20 FINANCIAL RESULTS")
    assert not is_results_release_title("Exhibit 99.1 XP INC. ANNOUNCES RESULTS")


# ---- a supplementary document in the history ---------------------------------------------------------------------------------------------


def statements(period, published, eps):
    return Announcement(pd.Period(period, freq="Q"), pd.Timestamp(published), f"https://example.com/{period}-statements.htm", "BRL", True, {"eps": [eps, eps]}, "US")


def press_release(period, published, adjusted):
    values = {} if adjusted is None else {"non_ifrs_eps": [adjusted]}
    return Announcement(pd.Period(period, freq="Q"), pd.Timestamp(published), f"https://example.com/{period}-release.htm", "BRL", True, values, "US", supplement=True)


QUARTERS = [("2025Q4", "2026-02-12 16:37"), ("2026Q1", "2026-05-18 16:17"), ("2026Q2", "2026-08-17 16:27"), ("2026Q3", "2026-11-16 16:11")]


def test_a_release_of_the_same_day_completes_the_row_and_adds_none():
    main = [statements(p, t, 2.0) for p, t in QUARTERS]
    alone = build_history(main)
    releases = [
        press_release("2025Q4", "2026-02-12 16:45", 2.58),  # after the statements
        press_release("2026Q1", "2026-05-18 16:05", 2.53),  # before them
        press_release("2026Q2", "2026-08-17 16:28", 2.71),
        press_release("2026Q3", "2026-11-16 16:13", 2.73),
    ]
    history = build_history(main + releases)
    pd.testing.assert_frame_equal(history.ttm.drop(columns="non_ifrs_eps_ttm"), alone.ttm.drop(columns="non_ifrs_eps_ttm"))
    assert history.ttm["non_ifrs_eps_ttm"][:3].isna().all()  # the year before 2025Q4 has no adjusted EPS
    assert history.ttm["non_ifrs_eps_ttm"].iloc[-1] == pytest.approx(2.58 + 2.53 + 2.71 + 2.73)
    q = history.quarters["non_ifrs_eps"][pd.Period("2025Q4")]
    assert q.published == pd.Timestamp("2026-02-12 16:45") and q.observations[0].url.endswith("2025Q4-release.htm")
    assert len(history.announcements) == 8


def test_a_release_on_a_later_day_adds_a_row_from_that_day_and_never_reaches_back():
    main = [statements(p, t, 2.0) for p, t in QUARTERS]
    releases = [press_release(p, t, 2.5) for p, t in QUARTERS[:3]] + [press_release("2026Q3", "2026-11-18 08:00", 2.73)]
    history = build_history(main + releases)
    ttm = history.ttm
    assert len(ttm) == 5
    assert pd.isna(ttm["non_ifrs_eps_ttm"].iloc[3])  # on the day of the statements the release was not out yet
    last = ttm.iloc[4]
    assert last["available_date"] == US.available_date(pd.Timestamp("2026-11-18 08:00")) and last["published"] == "2026-11-18 08:00"
    assert last["non_ifrs_eps_ttm"] == pytest.approx(2.5 * 3 + 2.73) and last["eps_ttm"] == ttm["eps_ttm"].iloc[3]
    assert "Non-GAAP EPS from the press release published 2026-11-18 08:00" in history.ttm_detail[4]["note"]


def test_a_quarter_without_an_adjusted_eps_leaves_the_ttm_empty_and_a_restated_value_keeps_the_first():
    main = [statements(p, t, 2.0) for p, t in QUARTERS]
    gap = [press_release("2025Q4", "2026-02-12 16:45", 2.58), press_release("2026Q1", "2026-05-18 16:05", None),
           press_release("2026Q2", "2026-08-17 16:28", 2.71), press_release("2026Q3", "2026-11-16 16:13", 2.73)]
    assert build_history(main + gap).ttm["non_ifrs_eps_ttm"].isna().all()
    again = [press_release(p, t, 2.5) for p, t in QUARTERS] + [press_release("2026Q3", "2026-11-20 16:00", 2.6)]
    history = build_history(main + again)
    assert history.quarters["non_ifrs_eps"][pd.Period("2026Q3")].value == 2.5
    assert any("Non-IFRS EPS 2026Q3 differs between filings" in d for d in history.discrepancies())


def test_a_supplement_is_stored_with_its_own_table_and_parser_version():
    a = press_release("2026Q3", "2026-11-16 16:13", 2.73)
    frame = announcements_to_frame([statements("2026Q3", "2026-11-16 16:11", 2.0), a], 1)
    assert frame["table"].tolist() == ["quarterly", "supplement"] and frame["parser_version"].tolist() == [1, SUPPLEMENT_PARSER_VERSION]
    back = announcements_from_frame(frame, US)
    assert [x.supplement for x in back] == [False, True] and back[1].values == {"non_ifrs_eps": [2.73]} and back[1].is_quarterly_table


def test_a_supplementary_filing_is_parsed_as_a_release_whatever_the_layout():
    filing = Filing("XP Inc. Reports Fourth Quarter 2024 Results", pd.Timestamp("2025-02-18 16:13"), "https://example.com/r.htm", FOURTH_QUARTER, supplement=True)
    a = parse_announcement(filing, "ifrs_statements", US)
    assert a.supplement and a.period == pd.Period("2024Q4") and a.values == {"non_ifrs_eps": [2.25]} and a.market == "US"


def test_the_press_release_is_read_only_next_to_ifrs_statements():
    base = {"symbol": "XP", "cik": 1787425, "metrics": ["pe", "pe_non_gaap"], "sec_release_non_gaap": True}
    assert parse_config({"stocks": [{**base, "filing_format": "ifrs_statements", "sec_exhibit": "ifrs_statements"}]}).stocks[0].sec_release_non_gaap
    with pytest.raises(ValueError, match="sec_release_non_gaap"):
        parse_config({"stocks": [{**base, "filing_format": "press_release"}]})
    assert not parse_config({"stocks": [{"symbol": "JD", "cik": 1549802}]}).stocks[0].sec_release_non_gaap


# ---- the saved press releases (data/filings/XP/releases.json, written by the scheduled update) --------------------------------------------

saved = pytest.mark.skipif(not (FOLDER / "releases.json").exists(), reason="no saved XP press releases (the scheduled update saves them)")
EXPECTED = {  # every Adjusted Basic EPS XP has stated (R$ per share); the other quarters state none
    "2024Q4": 2.25, "2025Q1": 2.31, "2025Q4": 2.58, "2026Q1": 2.53, "2026Q2": 2.71, "2026Q3": 2.73,
}


@saved
def test_every_saved_release_parses_and_only_the_stated_quarters_have_a_value():
    index = [e for e in json.loads((FOLDER / "releases.json").read_text(encoding="utf-8")) if e["results"]]
    parsed = {}
    for e in index:
        p = parse_adjusted_eps_release((FOLDER / e["file"]).read_bytes())
        assert str(p.period) not in parsed, f"two releases for {p.period}"
        parsed[str(p.period)] = p.values.get("non_ifrs_eps", [None])[0]
    # one release for every quarter from 2Q20 (the 1Q20 release is a letter to shareholders without a "Reports ... Results" title; 4Q19 may be missing
    # when EDGAR did not serve its filing index during an update, which is then tried again)
    assert [p for p in parsed if p != "2019Q4"] == [str(p) for p in pd.period_range("2020Q2", max(parsed), freq="Q")]
    assert {p: v for p, v in parsed.items() if v is not None} == {p: v for p, v in EXPECTED.items() if p <= max(parsed)}


@saved
def test_the_statements_selection_is_untouched_by_the_releases():
    statements_index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    releases = {e["file"] for e in json.loads((FOLDER / "releases.json").read_text(encoding="utf-8")) if e["results"]}
    assert not releases & {e.get("file") for e in statements_index}


def test_a_newer_record_without_a_value_ends_the_older_one_instead_of_carrying_it_on():
    from valuation_monitor.data import StockData
    from valuation_monitor.metrics import METRICS

    main = [statements(p, t, 2.0) for p, t in QUARTERS] + [statements("2026Q4", "2027-02-11 16:30", 2.0)]
    releases = [press_release(p, t, 2.5) for p, t in QUARTERS] + [press_release("2026Q4", "2027-02-11 16:40", None)]  # no adjustments in 2026Q4
    history = build_history(main + releases)
    days = pd.bdate_range("2026-11-16", "2027-02-16")
    stock = parse_config({"stocks": [{"symbol": "XP", "cik": 1787425, "metrics": ["pe", "pe_non_gaap"]}]}).stocks[0]
    data = StockData(stock, pd.Series(20.0, index=days), "BRL", pd.Series(1.0, index=days), history.ttm, history)
    pe = METRICS["pe_non_gaap"].compute(data)
    assert pe[pd.Timestamp("2026-11-17")] == pytest.approx(20 / 10.0) and pe[pd.Timestamp("2027-02-11")] == pytest.approx(2.0)
    assert pe[pd.Timestamp("2027-02-12"):].isna().all()  # the 2026Q3 TTM is not used for the quarter after it
    assert METRICS["pe"].compute(data)[pd.Timestamp("2027-02-12")] == pytest.approx(20 / 8.0)
