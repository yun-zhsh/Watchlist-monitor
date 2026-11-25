"""NVIDIA (NVDA): 8-K press releases in the us_press_release layout, with a fiscal year ending in late January (fiscal quarters filed under the
calendar quarter holding most of them), exhibits named after the fiscal quarter, a preliminary-results 8-K that is not a release, and the
Non-GAAP diluted EPS of the reconciliation, whose definition changed with the release of May 20, 2026."""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from valuation_monitor.config import load_config
from valuation_monitor.filings import PARSER_VERSIONS, Announcement, build_history
from valuation_monitor.providers.sec import SecProvider
from valuation_monitor.us_release import nvidia_non_gaap_eps, parse_us_release, quarter_of

ROOT = Path(__file__).resolve().parent.parent
NVDA = ROOT / "data" / "filings" / "NVDA"


def table(*rows):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def release(*tables):
    return ("<html><body><p>Exhibit 99.1</p><p>NVIDIA Announces Financial Results for Third Quarter Fiscal 2027</p>" + "".join(tables) + "</body></html>").encode()


UNIT = "<p>($ In millions, except per share data)</p>"
INCOME = UNIT + table(
    ["Three Months Ended", "Nine Months Ended"],
    ["October 25,", "October 26,", "October 25,", "October 26,"],
    ["2026", "2025", "2026", "2025"],
    ["Revenue", "115,844", "57,006", "293,680", "148,811"],
    ["Net income", "65,287", "31,910", "183,296", "77,107"],
    ["Net income per share:"],
    ["Basic", "2.71", "1.31", "7.58", "3.14"],
    ["Diluted", "2.70", "1.30", "7.55", "3.14"],
    ["Weighted average shares used in per share computation:"],
    ["Basic", "24,106", "24,327", "24,194", "24,394"],
    ["Diluted", "24,186", "24,483", "24,287", "24,542"],
)
BALANCE = UNIT + table(
    ["October 25, 2026", "January 25, 2026"],
    ["Total assets", "330,000", "206,803"],
    ["Total shareholders' equity", "266,666", "157,293"],
    ["Total liabilities and shareholders' equity", "330,000", "206,803"],
)
# the reconciliation is split over tables: the header is in the first, the per-share block in a later one
RECONCILIATION_HEAD = "<p>RECONCILIATION OF GAAP TO NON-GAAP FINANCIAL MEASURES</p>" + UNIT + table(
    ["Three Months Ended", "Nine Months Ended"],
    ["October 25,", "July 26,", "October 26,", "October 25,", "October 26,"],
    ["2026", "2026", "2025", "2026", "2025"],
    ["GAAP gross profit", "87,000", "71,000", "41,849", "219,000", "103,000"],
)
RECONCILIATION_TAIL = table(
    ["Diluted net income per share"],
    ["GAAP", "2.70", "2.46", "1.30", "7.55", "3.14"],
    ["Non-GAAP*", "2.68", "2.22", "1.27", "6.77", "3.05"],
    ["Weighted average shares used in diluted net income per share computation", "24,186", "24,285", "24,483", "24,287", "24,542"],
)


def soup(html: bytes):
    return BeautifulSoup(html, "lxml")


def test_fiscal_quarters_are_filed_under_the_calendar_quarter_holding_most_of_them():
    assert quarter_of(pd.Timestamp("2024-04-28")) == pd.Period("2024Q1")  # fiscal Q1 2025, January 29 to April 28, 2024
    assert quarter_of(pd.Timestamp("2024-07-28")) == pd.Period("2024Q2")
    assert quarter_of(pd.Timestamp("2024-10-27")) == pd.Period("2024Q3")
    assert quarter_of(pd.Timestamp("2025-01-26")) == pd.Period("2024Q4")  # fiscal Q4 2025
    assert quarter_of(pd.Timestamp("2016-05-01")) == pd.Period("2016Q1")  # a 53-week year: fiscal Q1 2017 ended May 1, 2016
    # calendar quarter ends and ASML's Sundays near them stay where they were
    assert quarter_of(pd.Timestamp("2026-09-30")) == pd.Period("2026Q3")
    assert quarter_of(pd.Timestamp("2022-07-03")) == pd.Period("2022Q2")
    assert quarter_of(pd.Timestamp("2022-01-02")) == pd.Period("2021Q4")
    assert quarter_of(pd.Timestamp("2026-03-31")) == pd.Period("2026Q1")


def test_a_release_parses_with_the_non_gaap_diluted_eps_of_this_quarter_last_year_and_the_previous_quarter():
    p = parse_us_release(release(INCOME, BALANCE, RECONCILIATION_HEAD, RECONCILIATION_TAIL), "diluted")
    assert p.period == pd.Period("2026Q3") and p.quarterly and p.currency == "USD"
    assert p.values["eps"] == [2.71, 1.31] and p.values["wa_shares"] == pytest.approx([24106, 24327])
    assert p.values["revenue"] == pytest.approx([115844, 57006]) and p.values["profit"] == pytest.approx([65287, 31910])
    assert p.values["non_ifrs_eps"] == [2.68, 1.27, 2.22]  # this quarter, same quarter last year, previous quarter
    # the comparison column of the balance sheet is the end of the fiscal year (January 25, 2026): the end of 2025Q4
    assert p.values["equity"] == pytest.approx([266666, 157293])


def test_the_non_gaap_eps_is_read_only_for_a_stock_with_a_diluted_non_gaap_pe():
    html = release(INCOME, BALANCE, RECONCILIATION_HEAD, RECONCILIATION_TAIL)
    assert "non_ifrs_eps" not in parse_us_release(html).values
    assert "non_ifrs_eps" not in parse_us_release(html, "basic").values


def test_a_first_quarter_reconciliation_with_its_own_header_and_an_alternative_row_that_is_not_taken():
    q1 = table(
        ["Three Months Ended"],
        ["April 27,", "January 26,", "April 28,"],
        ["2025", "2025", "2024"],
        ["Diluted net income per share"],
        ["GAAP", "0.76", "0.89", "0.60"],
        ["Non-GAAP", "0.81", "0.89", "0.61"],
    )
    alternative = table(
        ["Diluted net income per share"],
        ["GAAP", "0.76"],
        ["Non-GAAP (as adjusted to exclude H20 excess inventory and purchase obligation charges)", "0.96"],
    )
    income = UNIT + table(
        ["Three Months Ended"],
        ["April 27,", "April 28,"],
        ["2025", "2024"],
        ["Revenue", "44,062", "26,044"],
        ["Net income", "18,775", "14,881"],
        ["Net income per share:"],
        ["Basic", "0.77", "0.60"],
        ["Diluted", "0.76", "0.60"],
        ["Weighted average shares used in per share computation:"],
        ["Basic", "24,441", "24,620"],
    )
    assert nvidia_non_gaap_eps(soup(release(income, q1, alternative)), pd.Period("2025Q1")) == [0.81, 0.61, 0.89]


def test_a_gaap_row_that_does_not_match_the_income_statement_or_two_blocks_that_disagree_are_errors():
    wrong = RECONCILIATION_TAIL.replace(">2.70<", ">2.46<", 1)  # the GAAP row of another column: the header was misread
    with pytest.raises(ValueError, match="does not match the income statement"):
        nvidia_non_gaap_eps(soup(release(INCOME, RECONCILIATION_HEAD, wrong)), pd.Period("2026Q3"))
    other = RECONCILIATION_TAIL.replace(">2.68<", ">2.69<")
    with pytest.raises(ValueError, match="disagree"):
        nvidia_non_gaap_eps(soup(release(INCOME, RECONCILIATION_HEAD, RECONCILIATION_TAIL, other)), pd.Period("2026Q3"))
    assert nvidia_non_gaap_eps(soup(release(INCOME, BALANCE)), pd.Period("2026Q3")) is None  # no reconciliation block


def test_the_definition_change_makes_a_ttm_of_four_quarters_of_the_new_definition_with_the_restated_previous_quarter():
    def ann(period, published, eps, non_gaap):
        values = {"eps": [eps, eps, eps], "non_ifrs_eps": non_gaap}
        return Announcement(pd.Period(period, freq="Q"), pd.Timestamp(published), f"https://example.com/{period}.htm", "USD", True, values, "US")

    filings = [
        ann("2025Q1", "2025-05-28 16:21", 0.77, [0.81, 0.61, 0.89]),
        ann("2025Q2", "2025-08-27 16:22", 1.08, [1.05, 0.68, 0.81]),
        ann("2025Q3", "2025-11-19 16:20", 1.31, [1.30, 0.81, 1.05]),
        ann("2025Q4", "2026-02-25 16:31", 1.77, [1.62, 0.89, 1.30]),
        ann("2026Q1", "2026-05-20 16:21", 2.40, [1.87, 0.78, 1.59]),
        ann("2026Q2", "2026-08-26 16:21", 2.47, [2.22, 1.01, 1.87]),
        ann("2026Q3", "2026-11-17 16:20", 2.71, [2.68, 1.27, 2.22]),
    ]
    ttm = build_history(filings, non_gaap_changes=("2026-05-20",)).ttm
    by_published = dict(zip(ttm["published"], ttm["non_ifrs_eps_ttm"]))
    assert by_published["2026-02-25 16:31"] == pytest.approx(0.81 + 1.05 + 1.30 + 1.62)  # the old definition
    assert pd.isna(by_published["2026-05-20 16:21"]) and pd.isna(by_published["2026-08-26 16:21"])
    assert by_published["2026-11-17 16:20"] == pytest.approx(1.59 + 1.87 + 2.22 + 2.68)  # 2025Q4 as restated in May 2026


SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["8-K", "8-K", "8-K"],
            "items": ["2.02,9.01", "2.02,7.01,9.01", "2.02,9.01"],
            "accessionNumber": ["0001045810-22-000073", "0001045810-22-000133", "0001045810-22-000136"],
            "acceptanceDateTime": ["2022-05-25T20:22:25.000Z", "2022-08-08T13:16:50.000Z", "2022-08-24T20:24:34.000Z"],
        },
        "files": [],
    }
}
RELEASE = b"<html><body><p>EX-99.1 Exhibit 99.1</p><p>NVIDIA Announces Financial Results for First Quarter Fiscal 2023</p>" + b"<p>x</p>" * 20000 + b"</body></html>"
PRELIMINARY = b"<html><body><p>Exhibit 99.1</p><p>NVIDIA Announces Preliminary Financial Results for Second Quarter Fiscal 2023</p>" + b"<p>x</p>" * 20000 + b"</body></html>"


def listing(*files):
    return json.dumps({"directory": {"item": [{"name": n, "size": str(s)} for n, s in files]}}).encode()


class FakeNvidiaHttp:
    def __init__(self):
        self.requests = []

    def get_json(self, url, params=None):
        return SUBMISSIONS

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        if url.endswith("/000104581022000073/index.json"):
            return listing(("nvda-20220525.htm", 35_246), ("q1fy23cfocommentary.htm", 252_958), ("q1fy23pr.htm", 323_853), ("R1.htm", 38_849))
        if url.endswith("/000104581022000133/index.json"):
            return listing(("nvda-20220808.htm", 34_139), ("pressrelease.htm", 47_987), ("R1.htm", 38_697))
        if url.endswith("/000104581022000136/index.json"):
            return listing(("nvda-20220824.htm", 36_004), ("q2fy23cfocommentary.htm", 297_445), ("q2fy23pr.htm", 399_315))
        if url.endswith("/q1fy23pr.htm"):
            return RELEASE
        if url.endswith("/q2fy23pr.htm"):
            return RELEASE.replace(b"First", b"Second")
        if url.endswith("/pressrelease.htm"):
            return PRELIMINARY
        raise AssertionError(f"unexpected request: {url}")


def test_exhibits_named_after_the_fiscal_quarter_are_found_and_a_preliminary_results_8k_is_not_a_release(tmp_path):
    http = FakeNvidiaHttp()
    found = SecProvider(http, tmp_path).results_announcements("NVDA", 1045810, datetime(2022, 1, 1), "8-K")
    assert [f.published for f in found] == [pd.Timestamp("2022-05-25 16:22:25"), pd.Timestamp("2022-08-24 16:24:34")]
    assert all(f.title.endswith("NVIDIA Announces Financial Results") for f in found)
    index = json.loads((tmp_path / "NVDA" / "index.json").read_text(encoding="utf-8"))
    assert [e["results"] for e in index] == [True, False, True]
    assert not any("cfocommentary" in url or url.endswith("/nvda-20220525.htm") for url in http.requests)  # the CFO commentary and the 8-K are never opened


def test_config():
    stocks = load_config(ROOT / "config.yaml").stocks
    stock = next(s for s in stocks if s.symbol == "NVDA")
    assert stock.name == "NVIDIA" and stock.market == "US" and stock.cik == 1045810 and stock.sec_form == "8-K"
    assert stock.filing_format == "us_press_release" and stock.metrics == ["pe", "pe_non_gaap", "ps", "pb"]
    assert stock.eps_basis == {"pe_non_gaap": "diluted"} and stock.non_gaap_changes == ("2026-05-20",)
    symbols = [s.symbol for s in stocks]
    assert symbols.index("NVDA") < symbols.index("META")
    assert PARSER_VERSIONS["us_press_release"] >= 6 and PARSER_VERSIONS["us_gaap_statements"] >= 5  # quarter_of and the NVIDIA rules


saved = pytest.mark.skipif(not (NVDA / "index.json").exists(), reason="no saved NVIDIA filings (the scheduled update saves them)")


def parsed_releases():
    index = [e for e in json.loads((NVDA / "index.json").read_text(encoding="utf-8")) if e["results"]]
    return {p.period: p for p in (parse_us_release((NVDA / e["file"]).read_bytes(), "diluted") for e in index)}


@saved
def test_every_saved_nvidia_release_parses_with_its_non_gaap_eps():
    by_period = parsed_releases()
    periods = sorted(by_period)
    assert periods[0] == pd.Period("2014Q4") and periods[-1] >= pd.Period("2026Q3")  # fiscal Q4 2015 (January 25, 2015) on
    assert periods == list(pd.period_range(periods[0], periods[-1], freq="Q"))  # no quarter missing, none twice
    for period, p in by_period.items():
        assert p.currency == "USD" and p.quarterly, period
        assert len(p.values["non_ifrs_eps"]) == 3, period  # every release states a Non-GAAP diluted EPS
        assert len(p.values["equity"]) == 2, period  # the comparison column is the end of the fiscal year: the previous calendar Q4
        for col in (0, 1):
            profit, eps, shares = p.values["profit"][col], p.values["eps"][col], p.values["wa_shares"][col]
            rounding = 0.005 + 0.5 / shares  # EPS to the cent, net income to the million (2015-2016: 137 / 559 = 0.245, published 0.24)
            assert abs(profit / shares - eps) <= rounding + 1e-9 or (str(period), col) == ("2024Q4", 1), (period, col)
    # NVIDIA's release of February 2025 prints 0.51 as the basic EPS of fiscal Q4 2024, while 12,285 / 24,660 is 0.498
    assert by_period[pd.Period("2024Q4")].values["eps"][1] == 0.51


@saved
def test_prior_year_columns_agree_except_the_splits_the_2016_revision_and_the_new_non_gaap_definition():
    by_period = parsed_releases()
    split_on = [(pd.Timestamp("2021-07-20"), 4), (pd.Timestamp("2024-06-10"), 10)]
    differing = set()
    for period, p in by_period.items():
        later = by_period.get(period + 4)
        if later is None:
            continue
        end = period.end_time + pd.Timedelta(days=60)  # about when the quarter's own release came out; a split within the year after it divides its EPS
        factor = 1
        for day, ratio in split_on:
            if end < day < end + pd.DateOffset(years=1):
                factor *= ratio
        for key in ("revenue", "profit", "eps", "non_ifrs_eps"):
            per_share = "eps" in key
            first, again = p.values[key][0] / (factor if per_share else 1), later.values[key][1]
            if abs(first - again) > (0.006 if per_share else 0.5):
                differing.add((str(period), key))
    assert differing == {
        ("2016Q1", "profit"), ("2016Q1", "eps"), ("2016Q2", "profit"), ("2016Q2", "eps"),  # revised in the fiscal 2018 releases
        ("2023Q4", "eps"),  # 4.98 / 10 = 0.498, printed 0.51 in February 2025
        ("2025Q1", "non_ifrs_eps"), ("2025Q2", "non_ifrs_eps"), ("2025Q3", "non_ifrs_eps"),  # restated to include stock-based compensation
    }


@saved
def test_nvidia_known_values():
    by_period = parsed_releases()
    q = by_period[pd.Period("2026Q3")]
    assert q.values["revenue"] == pytest.approx([115844, 57006]) and q.values["non_ifrs_eps"] == [2.68, 1.27, 2.22]
    assert q.values["equity"] == pytest.approx([266666, 157293])
    assert by_period[pd.Period("2026Q1")].values["non_ifrs_eps"] == [1.87, 0.78, 1.59]  # the first release of the new definition
    assert by_period[pd.Period("2025Q1")].values["non_ifrs_eps"] == [0.81, 0.61, 0.89]  # not the alternative "as adjusted" 0.96
