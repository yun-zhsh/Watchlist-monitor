"""Tesla (TSLA): the quarterly Update deck (exhibit 99.1 of its 8-K) read as text in the tesla_update layout, the Form 10-Q or 10-K of a quarter
whose deck has its statements as page images only, the diluted Non-GAAP EPS and the note on the definition used until the second quarter of 2016."""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.config import load_config, parse_config
from valuation_monitor.filings import PARSER_VERSIONS, adjust_for_splits, announcements_from_frame, announcements_to_frame, build_history, parse_announcement
from valuation_monitor.market_calendar import get_market
from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.sec import IMAGES_ONLY, SecProvider, periodic_report_title, tesla_update_title
from valuation_monitor.tesla_update import (
    OLD_DEFINITION_NOTE,
    parse_tesla_filing,
    parse_tesla_update,
    statements_in_text,
    title_quarter,
)

ROOT = Path(__file__).resolve().parent.parent
TSLA = ROOT / "data" / "filings" / "TSLA"
US = get_market("US")


def deck(*parts: str) -> bytes:
    return ("<html><body>" + "".join(f"<p>{p}</p>" for p in parts) + "</body></html>").encode()


# the five-quarter layout (the third quarter of 2019 on): financial summary first, then the statements, then the reconciliation
SUMMARY_2026 = (
    "F I N A N C I A L S U M M A R Y (Unaudited) ($ in millions, except percentages and per share data) Q3-2025 Q4-2025 Q1-2026 Q2-2026 Q3-2026 YoY "
    "Total revenues 28,095 24,901 22,387 28,236 28,046 0% "
    "EPS attributable to common stockholders, diluted (GAAP) 0.39 0.24 0.13 0.32 0.22 -44% "
    "EPS attributable to common stockholders, diluted (non-GAAP) 0.50 0.50 0.41 0.33 0.48 -4%"
)
STATEMENTS_2026 = (
    "F I N A N C I A L S T A T E M E N T S In millions of USD or shares as applicable, except per share data Q3-2025 Q4-2025 Q1-2026 Q2-2026 Q3-2026 "
    "REVENUES Automotive sales 20,359 16,750 15,473 20,006 20,094 Total revenues 28,095 24,901 22,387 28,236 28,046 "
    "COST OF REVENUES Total cost of revenues 23,041 19,892 17,667 23,485 22,552 Gross profit 5,054 5,009 4,720 4,751 5,494 "
    "Restructuring and other 238 162 — — — Other (expense) income, net (1) (28) (592) (535) 590 (117) "
    "NET INCOME (1) 1,389 856 491 1,128 804 "
    "Net income attributable to noncontrolling interests and redeemable noncontrolling interests in subsidiaries 16 16 14 14 17 "
    "NET INCOME ATTRIBUTABLE TO COMMON STOCKHOLDERS (1) 1,373 840 477 1,114 787 Less: Buy-out of noncontrolling interest - - - (2) — "
    "NET INCOME USED IN COMPUTING NET INCOME PER SHARE OF COMMON STOCK 1,373 840 477 1,116 787 "
    "Net income per share of common stock attributable to common stockholders Basic (1) $ 0.43 $ 0.26 $ 0.15 $ 0.34 $ 0.24 "
    "Diluted (1) $ 0.39 $ 0.24 $ 0.13 $ 0.32 $ 0.22 "
    "Weighted average shares used in computing net income per share of common stock Basic 3,227 3,231 3,234 3,237 3,240 Diluted 3,526 3,539 3,538 3,540 3,541 "
    "S T A T E M E N T O F O P E R A T I O N S (Unaudited) 27 B A L A N C E S H E E T (Unaudited) In millions of USD "
    "30-Sep-25 31-Dec-25 31-Mar-26 30-Jun-26 30-Sep-26 ASSETS Total assets 133,735 137,806 143,724 148,524 157,517 "
    "Redeemable noncontrolling interests in subsidiaries 59 58 57 54 54 Total stockholders' equity 79,970 82,137 84,116 86,858 89,289 "
    "Noncontrolling interests in subsidiaries 687 670 629 607 546"
)
RECONCILIATION_2026 = (
    "R E C O N C I L I A T I O N In millions of USD or shares as applicable, except per share data Q3-2025 Q4-2025 Q1-2026 Q2-2026 Q3-2026 "
    "Net income attributable to common stockholders (GAAP) 1,373 840 477 1,114 787 Stock-based compensation expense, net of tax 459 682 803 989 982 "
    "Net income attributable to common stockholders (non-GAAP) 1,770 1,761 1,453 1,153 1,697 "
    "EPS attributable to common stockholders, diluted (GAAP) 0.39 0.24 0.13 0.32 0.22 "
    "EPS attributable to common stockholders, diluted (non-GAAP) 0.50 0.50 0.41 0.33 0.48"
)
DECK_2026 = deck("EX-99.1 2 exhibit9914.htm EX-99.1 exhibit9914 Q3 2026 Update 1 Exhibit 99.1", SUMMARY_2026, STATEMENTS_2026, RECONCILIATION_2026)


def test_the_five_quarter_deck_this_quarter_last_year_and_the_previous_quarter():
    p = parse_tesla_update(DECK_2026, "diluted")
    assert p.period == pd.Period("2026Q3") and p.quarterly and p.currency == "USD" and p.notes is None
    assert p.values["revenue"] == [28046, 28095, 28236]
    assert p.values["profit"] == [787, 1373, 1114]  # attributable to common stockholders, not the total net income nor the figure used for EPS
    assert p.values["eps"] == [0.24, 0.43, 0.34] and p.values["wa_shares"] == [3240, 3227, 3237]
    assert p.values["equity"] == [89289, 82137]  # Tesla's own stockholders' equity at September 30, 2026 and December 31, 2025
    assert p.values["non_ifrs_eps"] == [0.48, 0.50, 0.33]  # the diluted Non-GAAP EPS of the reconciliation


def test_the_non_gaap_eps_is_read_only_for_a_diluted_non_gaap_pe():
    assert "non_ifrs_eps" not in parse_tesla_update(DECK_2026).values
    with pytest.raises(ValueError, match="diluted Non-GAAP EPS only"):
        parse_tesla_update(DECK_2026, "basic")


# the shareholder letter of 2015: three months and the year in one header, thousands, a loss, "basic and diluted", and the earlier definition
LETTER_2014Q4 = deck(
    "EX-99.1 2 d853966dex991.htm EXHIBIT 99.1 Exhibit 99.1 Exhibit 99.1 Tesla Motors – Fourth Quarter & Full Year 2014 Shareholder Letter",
    "Tesla Motors, Inc. Condensed Consolidated Statements of Operations (Unaudited) (In thousands, except per share data) Three Months Ended Year Ended "
    "Dec 31, 2014 Sep 30, 2014 Dec 31, 2013 Dec 31, 2014 Dec 31, 2013 Revenues Automotive sales (1A) $ 956,661 $ 849,009 $ 610,851 $ 3,192,723 $ 1,997,786 "
    "Total revenues 956,661 851,804 615,219 3,198,356 2,013,496 Cost of revenues Total cost of revenues (2) 715,301 610,002 461,240 2,316,685 1,557,234 "
    "Net loss $ (107,629 ) $ (74,708 ) $ (16,264 ) $ (294,040 ) $ (74,014 ) "
    "Net loss per common share, basic and diluted $ (0.86 ) $ (0.60 ) $ (0.13 ) $ (2.36 ) $ (0.62 ) "
    "Shares used in per share calculation, basic and diluted 125,497 124,911 122,802 124,573 119,421",
    "Tesla Motors, Inc. Condensed Consolidated Balance Sheets (Unaudited) (In thousands) Dec 31, Dec 31, 2014 2013 Assets Total assets $ 5,849,251 $ 2,416,930 "
    "Liabilities and Stockholders’ Equity Total liabilities 4,879,345 1,749,809 Stockholders’ equity 911,710 667,120 Total liabilities and stockholders’ equity $ 5,849,251 $ 2,416,930",
    "Reconciliation of GAAP to Non-GAAP Financial Information (Unaudited) (In thousands, except per share data) Three Months Ended Year Ended "
    "Dec 31, 2014 Sep 30, 2014 Dec 31, 2013 Dec 31, 2014 Dec 31, 2013 Net loss (GAAP) $ (107,629 ) $ (74,708 ) $ (16,264 ) $ (294,040 ) $ (74,014 ) "
    "Stock-based compensation expense 44,517 39,158 28,089 156,496 83,655 Non-cash interest expense related to convertible notes 20,826 22,160 4,299 75,019 10,350 "
    "Model S gross profit deferred due to lease accounting (1)(2) 26,072 16,564 29,796 82,626 77,878 "
    "Net income (loss) per share, basic (Non-GAAP) $ (0.13 ) $ 0.02 $ 0.37 $ 0.16 $ 0.87 "
    "Net income (loss) per share, diluted (Non-GAAP) $ (0.13 ) $ 0.02 $ 0.33 $ 0.14 $ 0.78",
)


def test_the_2015_letter_three_months_and_year_columns_thousands_and_a_loss():
    p = parse_tesla_update(LETTER_2014Q4, "diluted")
    assert p.period == pd.Period("2014Q4") and p.quarterly
    assert p.values["revenue"] == pytest.approx([956.661, 615.219, 851.804])  # the three-months columns only, converted to millions
    assert p.values["profit"] == pytest.approx([-107.629, -16.264, -74.708])
    assert p.values["eps"] == [-0.86, -0.13, -0.60] and p.values["wa_shares"] == pytest.approx([125.497, 122.802, 124.911])
    assert p.values["equity"] == pytest.approx([911.71, 667.12])
    assert p.values["non_ifrs_eps"] == [-0.13, 0.33, 0.02]  # diluted, not the basic row above it
    assert p.notes == {"non_ifrs_eps": OLD_DEFINITION_NOTE}  # the reconciliation has the items of the definition used until the second quarter of 2016


DECK_2016Q3 = deck(
    "Exhibit 99.1 Tesla Third Quarter 2016 Update",
    "Tesla Motors, Inc. Condensed Consolidated Statements of Operations (Unaudited) (In thousands, except per share data) Three Months Ended Nine Months Ended "
    "Sept 30, June 30, Sept 30, Sept 30, Sept 30, 2016 2016 2015 2016 2015 Revenues Total revenues 2,298,436 1,270,017 936,789 5,715,501 2,831,426 "
    "Cost of revenues Total cost of revenues 1,662,172 1,018,395 718,474 4,205,520 2,149,893 Net income (loss) $ 21,878 $ (293,188 ) $ (229,858 ) $ (553,577 ) $ (568,266 ) "
    "Net income (loss) per common share: Basic $ 0.15 $ (2.09 ) $ (1.78 ) $ (3.94 ) $ (4.47 ) Diluted $ 0.14 $ (2.09 ) $ (1.78 ) $ (3.94 ) $ (4.47 ) "
    "Shares used in per share calculation: Basic 148,991 139,983 129,006 140,581 127,225 Diluted 156,935 139,983 129,006 140,581 127,225",
    "Condensed Consolidated Balance Sheets (Unaudited) (In thousands) Sept 30, 2016 Dec 31, 2015 Assets Total assets $ 12,592,397 $ 8,067,939 "
    "Liabilities and Stockholders’ Equity Stockholders’ equity 2,680,488 1,083,704",
    "Reconciliation of GAAP to Non-GAAP Financial Information (Unaudited) (In thousands, except per share data) Three Months Ended Nine Months Ended "
    "Sept 30, June 30, Sept 30, Sept 30, Sept 30, 2016 2016 2015 2016 2015 Net Income (loss) - GAAP $ 21,878 $ (293,188 ) $ (229,858 ) $ (553,577 ) $ (568,266 ) "
    "Stock-based compensation expense 89,543 67,312 56,033 246,512 142,393 Net income (loss) per share, basic - Non-GAAP $ 0.75 $ (1.61 ) $ (1.35 ) $ (2.18 ) $ (3.35 ) "
    "Net income (loss) per share, diluted - Non-GAAP $ 0.71 $ (1.61 ) $ (1.35 ) $ (2.18 ) $ (3.35 )",
)


def test_days_over_years_and_basic_rows_under_a_heading_and_the_new_definition_has_no_note():
    p = parse_tesla_update(DECK_2016Q3, "diluted")
    assert p.period == pd.Period("2016Q3")
    assert p.values["eps"] == [0.15, -1.78, -2.09] and p.values["wa_shares"] == pytest.approx([148.991, 129.006, 139.983])
    assert p.values["equity"] == pytest.approx([2680.488, 1083.704]) and p.values["non_ifrs_eps"] == [0.71, -1.35, -1.61]
    assert p.notes is None  # stock-based compensation only: today's kind of definition


def test_a_deck_with_only_a_basic_non_gaap_eps_gives_none_and_one_with_neither_is_an_error():
    basic_only = DECK_2026.replace(b"diluted (non-GAAP)", b"basic (non-GAAP)")
    assert "non_ifrs_eps" not in parse_tesla_update(basic_only, "diluted").values  # the fourth quarter of 2019 and the first quarter of 2020
    neither = DECK_2026.replace(b"diluted (non-GAAP)", b"diluted (GAAP again)")
    with pytest.raises(ValueError, match="diluted Non-GAAP EPS"):
        parse_tesla_update(neither, "diluted")


IMAGE_DECK = deck("EX-99.1 2 tsla-ex991_99.htm EX-99.1 Q4 and FY2020 Update Exhibit 99.1 Highlights 03 Financial Summary 04 Financial Statements 24", "<img src='slide-1.jpg'>")


def test_an_image_only_deck_is_recognised_and_is_not_parsed():
    assert statements_in_text(DECK_2026) and statements_in_text(LETTER_2014Q4)
    assert not statements_in_text(IMAGE_DECK)
    with pytest.raises(ValueError, match="page images"):
        parse_tesla_update(IMAGE_DECK, "diluted")


def test_a_missing_row_is_an_error_not_a_guess():
    with pytest.raises(ValueError, match="basic EPS"):
        parse_tesla_update(DECK_2026.replace(b"Net income per share of common stock", b"Net income of common stock"))


# ---- Forms 10-Q and 10-K ------------------------------------------------------------------------------------------------------------------

REPORT_10Q = deck(
    "UNITED STATES SECURITIES AND EXCHANGE COMMISSION FORM 10-Q (Mark One) QUARTERLY REPORT For the quarterly period ended June 30, 2021 Tesla, Inc.",
    "Consolidated Balance Sheets (in millions, except per share data) (unaudited) June 30, 2021 December 31, 2020 Assets Total assets $ 57,834 $ 52,148 "
    "Accumulated deficit ( 3,608 ) ( 5,399 ) Total stockholders' equity 24,804 22,225 Noncontrolling interests in subsidiaries 841 850",
    "Consolidated Statements of Operations (in millions, except per share data) (unaudited) Three Months Ended June 30, Six Months Ended June 30, "
    "2021 2020 2021 2020 Revenues Total revenues 11,958 6,036 22,347 12,021 Cost of revenues Total cost of revenues 9,074 4,769 16,909 9,520 "
    "Net income 1,178 129 1,639 197 Net income attributable to common stockholders $ 1,142 $ 104 $ 1,580 $ 120 "
    "Net income per share of common stock attributable to common stockholders (1) Basic $ 1.18 $ 0.11 $ 1.64 $ 0.13 Diluted $ 1.02 $ 0.10 $ 1.41 $ 0.12 "
    "Weighted average shares used in computing net income per share of common stock (1) Basic 971 928 966 921 Diluted 1,119 1,036 1,126 1,016",
    "Consolidated Statements of Comprehensive Income (in millions) (unaudited) Net income $ 1,178 $ 129",
)
REPORT_10K = deck(
    "FORM 10-K ANNUAL REPORT For the fiscal year ended December 31, 2016 Tesla, Inc.",
    "Table of contents and the business section " + "text " * 100,
    "Consolidated Statements of Operations (in thousands, except per share data) Year Ended December 31, 2016 2015 2014 Revenues Total revenues 7,000,132 4,046,025 3,198,356 "
    "Cost of revenues Total cost of revenues 5,400,875 3,122,522 2,316,685 Net loss (773,046 ) (888,663 ) (294,040 ) "
    "Net loss attributable to noncontrolling interests and redeemable noncontrolling interests (98,132 ) — — Net loss attributable to common stockholders $ (674,914 ) $ (888,663 ) $ (294,040 ) "
    "Net loss per share of common stock attributable to common stockholders, basic and diluted $ (4.68 ) $ (6.93 ) $ (2.36 ) "
    "Weighted average shares used in computing net loss per share of common stock, basic and diluted 144,212 128,202 124,539",
    "Consolidated Statements of Comprehensive Loss (in thousands) Year Ended December 31, 2016 2015 2014",
    "Consolidated Balance Sheets (in thousands, except per share data) December 31, December 31, 2016 2015 Total stockholders’ equity 4,752,911 1,083,704",
)


def report(title: str, content: bytes, published: str = "2021-07-27 06:47") -> Filing:
    return Filing(title, pd.Timestamp(published), "https://www.sec.gov/Archives/edgar/data/1318605/x/report.htm", content)


def test_a_10q_balance_sheet_first_and_the_three_months_half_of_a_two_group_header():
    p = parse_tesla_filing(report("Form 10-Q for the quarterly period ended June 30, 2021", REPORT_10Q), "diluted")
    assert p.period == pd.Period("2021Q2") and p.quarterly
    assert p.values == {"revenue": [11958, 6036], "profit": [1142, 104], "eps": [1.18, 0.11], "wa_shares": [971, 928], "equity": [24804, 22225]}
    assert "non_ifrs_eps" not in p.values  # a report has no Non-GAAP EPS
    assert set(p.notes) == {"revenue", "profit", "eps"} and "; " not in p.notes["eps"] and p.notes["eps"].startswith("from the Form 10-Q, because the statements of the Update")


def test_a_10k_gives_the_full_year():
    p = parse_tesla_filing(report("Form 10-K for the fiscal year ended December 31, 2016", REPORT_10K, "2017-03-01 16:54"))
    assert p.period == pd.Period("2016Q4") and not p.quarterly  # the fourth quarter is the full year minus the first three (build_history)
    assert p.values["revenue"] == pytest.approx([7000.132, 4046.025]) and p.values["profit"] == pytest.approx([-674.914, -888.663])
    assert p.values["eps"] == [-4.68, -6.93] and p.values["equity"] == pytest.approx([4752.911, 1083.704])
    assert p.notes["revenue"].startswith("from the Form 10-K")


def test_a_report_for_another_period_than_its_title_is_an_error():
    with pytest.raises(ValueError, match="the report for 2021Q3"):
        parse_tesla_filing(report("Form 10-Q for the quarterly period ended September 30, 2021", REPORT_10Q))


def test_the_old_definition_note_reaches_the_ttm_formula_and_the_saved_frame():
    a = parse_announcement(Filing("Tesla Motors – Fourth Quarter & Full Year 2014 Shareholder Letter", pd.Timestamp("2015-02-11 17:29"), "u", LETTER_2014Q4), "tesla_update", US, "diluted")
    assert a.series_notes == {"non_ifrs_eps": OLD_DEFINITION_NOTE}
    history = build_history(adjust_for_splits([a], pd.Series(dtype=float)))
    assert history.quarters["non_ifrs_eps"][pd.Period("2014Q4")].note == OLD_DEFINITION_NOTE
    assert "; " not in OLD_DEFINITION_NOTE and announcements_from_frame(announcements_to_frame([a], 1), US)[0].series_notes == a.series_notes  # kept whole when saved


# ---- the SEC selection: the Update decks by their title, the image-only ones recorded, and the reports of those quarters only -------------


@pytest.mark.parametrize(
    "start, title, quarter",
    [
        ("EX-99.1 2 exhibit9914.htm EX-99.1 exhibit9914 Q3 2026 Update 1 Exhibit 99.1 Highlights", "Q3 2026 Update", "2026Q3"),
        ("EX-99.1 2 tsla-ex991_340.htm EX-99.1 tsla-ex991_340.pptx.htm Q4 and FY2019 Update Exhibit 99.1", "Q4 and FY2019 Update", "2019Q4"),
        ("EX-99.1 2 tsla-ex99_1.htm EX-99.1 Q4 and FY 2023 Update Exhibit 99.1", "Q4 and FY 2023 Update", "2023Q4"),
        ("EX-99.1 EX-99.1 Exhibit 99.1 Tesla Fourth Quarter & Full Year 2015 Update • $179 million", "Tesla Fourth Quarter & Full Year 2015 Update", "2015Q4"),
        ("Exhibit 99.1 Tesla Motors – First Quarter 2015 Shareholder Letter • Produced", "Tesla Motors – First Quarter 2015 Shareholder Letter", "2015Q1"),
    ],
)
def test_the_title_of_an_update_deck(start, title, quarter):
    assert tesla_update_title(deck(start)) == title and title_quarter(title) == pd.Period(quarter)


def test_production_and_deliveries_releases_are_not_decks():
    assert tesla_update_title(deck("EX-99.1 Exhibit 99.1 Tesla Q4 2020 Vehicle Production & Deliveries In 2020, we produced and delivered half")) is None
    assert tesla_update_title(deck("Exhibit 99.1 Tesla Vehicle Production & Deliveries and Date for Financial Results & Webcast for Second Quarter 2023")) is None


def test_the_title_of_a_periodic_report():
    assert periodic_report_title(REPORT_10Q, "10-Q") == "Form 10-Q for the quarterly period ended June 30, 2021"
    assert periodic_report_title(REPORT_10K, "10-K") == "Form 10-K for the fiscal year ended December 31, 2016"
    assert periodic_report_title(deck("FORM 8-K Current report"), "10-Q") is None


SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["8-K", "8-K", "8-K", "10-Q", "10-Q", "10-K"],
            "items": ["2.02", "2.02,9.01", "2.02", "", "", ""],
            "accessionNumber": ["0001628280-26-000001", "0001564590-21-000002", "0001564590-21-000003", "0001564590-21-000004", "0001628280-26-000005", "0001564590-22-000006"],
            "acceptanceDateTime": ["2026-10-21T20:41:42.000Z", "2021-07-26T20:07:00.000Z", "2021-07-02T13:00:00.000Z", "2021-07-27T10:47:13.000Z", "2026-10-23T12:00:00.000Z", "2022-02-07T12:00:00.000Z"],
            "primaryDocument": ["tsla-8k_20261021.htm", "tsla-8k_20210726.htm", "tsla-8k_20210702.htm", "tsla-20210630.htm", "tsla-20260930.htm", "tsla-20211231.htm"],
            "reportDate": ["2026-10-21", "2021-07-26", "2021-07-02", "2021-06-30", "2026-09-30", "2021-12-31"],
        },
        "files": [],
    }
}
PRODUCTION_RELEASE = deck("EX-99.1 Exhibit 99.1 Tesla Q2 2021 Vehicle Production & Deliveries", "x" * 25_000)


class FakeTeslaHttp:
    def __init__(self):
        self.requests = []

    def get_json(self, url, params=None):
        return SUBMISSIONS

    def get_bytes(self, url, **kwargs):
        self.requests.append(url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1])
        files = {
            "000162828026000001/index.json": json.dumps({"directory": {"item": [{"name": "tsla-8k_20261021.htm", "size": "9000"}, {"name": "exhibit9914.htm", "size": str(len(DECK_2026) + 20_000)}]}}).encode(),
            "000162828026000001/exhibit9914.htm": DECK_2026 + b" " * 20_000,
            "000156459021000002/index.json": json.dumps({"directory": {"item": [{"name": "exhibit.htm", "size": "29390"}]}}).encode(),
            "000156459021000002/exhibit.htm": IMAGE_DECK.replace(b"Q4 and FY2020", b"Q2 2021") + b" " * 29_000,
            "000156459021000003/index.json": json.dumps({"directory": {"item": [{"name": "tsla-ex991_7.htm", "size": "25100"}]}}).encode(),
            "000156459021000003/tsla-ex991_7.htm": PRODUCTION_RELEASE,
            "000156459021000004/tsla-20210630.htm": REPORT_10Q,
        }
        key = url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1]
        if key not in files:
            raise AssertionError(f"unexpected request: {url}")  # the reports of quarters with a readable deck must not be opened
        return files[key]


def test_decks_by_title_image_only_ones_recorded_and_the_report_of_their_quarter_only(tmp_path):
    http = FakeTeslaHttp()
    provider = SecProvider(http, tmp_path)
    decks = provider.results_announcements("TSLA", 1318605, datetime(2021, 1, 1), "8-K", "tesla_update")
    assert [(f.title, f.url.rsplit("/", 1)[-1]) for f in decks] == [("Q3 2026 Update", "exhibit9914.htm")]  # the image-only deck is not a result
    index = {e["accession"]: e for e in json.loads((tmp_path / "TSLA" / "index.json").read_text(encoding="utf-8"))}
    assert index["0001564590-21-000002"] == {"accession": "0001564590-21-000002", "published": "2021-07-26T16:07:00", "results": False, "title": "Q2 2021 Update", "statements": IMAGES_ONLY}
    assert index["0001564590-21-000003"]["results"] is False and "title" not in index["0001564590-21-000003"]  # a production release (named like an exhibit 99.1)

    reports = provider.results_announcements("TSLA", 1318605, datetime(2021, 1, 1), "10-Q/10-K", "periodic_report")
    assert [(f.title, f.published) for f in reports] == [("Form 10-Q for the quarterly period ended June 30, 2021", pd.Timestamp("2021-07-27 06:47:13"))]
    saved = json.loads((tmp_path / "TSLA" / "reports.json").read_text(encoding="utf-8"))
    assert [e["accession"] for e in saved] == ["0001564590-21-000004"]  # the 10-Q of 2026Q3 and the 10-K of 2021 are not wanted, and not recorded
    assert (tmp_path / "TSLA" / saved[0]["file"]).read_bytes() == REPORT_10Q
    assert "000156459021000004/tsla-20210630.htm" in http.requests and not any("tsla-20260930" in r or "tsla-20211231" in r for r in http.requests)


# ---- configuration ------------------------------------------------------------------------------------------------------------------------


def test_config_tesla():
    stock = next(s for s in load_config(ROOT / "config.yaml").stocks if s.symbol == "TSLA")
    assert stock.name == "Tesla" and stock.market == "US" and stock.cik == 1318605 and stock.sec_form == "8-K"
    assert stock.sec_exhibit == "tesla_update" and stock.filing_format == "tesla_update" and PARSER_VERSIONS["tesla_update"] >= 1
    assert stock.metrics == ["pe", "pe_non_gaap", "ps", "pb"] and stock.eps_basis == {"pe_non_gaap": "diluted"} and stock.non_gaap_changes == ()


def test_the_deck_exhibit_and_the_layout_go_together():
    base = {"symbol": "TSLA", "market": "US", "cik": 1318605, "sec_form": "8-K"}
    with pytest.raises(ValueError, match="together"):
        parse_config({"stocks": [{**base, "filing_format": "tesla_update"}]})
    with pytest.raises(ValueError, match="together"):
        parse_config({"stocks": [{**base, "sec_exhibit": "tesla_update", "filing_format": "us_press_release"}]})


# ---- the saved filings (skipped until the scheduled update has saved them) ------------------------------------------------------------

saved = pytest.mark.skipif(not (TSLA / "index.json").exists(), reason="no saved Tesla filings (the scheduled update saves them)")


def saved_filings() -> list[tuple[dict, object]]:
    index = [e for e in json.loads((TSLA / "index.json").read_text(encoding="utf-8")) if e["results"]]
    reports = [e for e in json.loads((TSLA / "reports.json").read_text(encoding="utf-8")) if e["results"]]
    out = []
    for e in index + reports:
        f = Filing(e["title"], pd.Timestamp(e["published"]), e["url"], (TSLA / e["file"]).read_bytes())
        out.append((e, parse_tesla_filing(f, "diluted")))
    return out


@saved
def test_every_saved_tesla_filing_parses_and_every_quarter_is_there_once():
    parsed = saved_filings()
    periods = sorted(p.period for _, p in parsed)
    assert periods[0] == pd.Period("2014Q4") and periods[-1] >= pd.Period("2026Q3")
    assert periods == list(pd.period_range(periods[0], periods[-1], freq="Q"))  # a deck or, for an image-only deck, a report: never both
    images = {title_quarter(e["title"]) for e in json.loads((TSLA / "index.json").read_text(encoding="utf-8")) if e.get("statements") == IMAGES_ONLY}
    assert {str(q) for q in images} >= {"2016Q4", "2020Q4", "2021Q1", "2021Q2", "2021Q3", "2021Q4", "2022Q1", "2022Q2", "2022Q3", "2022Q4", "2023Q1"}
    for e, p in parsed:
        from_report = e["title"].startswith("Form 10-")
        assert (p.period in images) == from_report, e["title"]
        assert p.quarterly == (not e["title"].startswith("Form 10-K")), e["title"]
        assert ("non_ifrs_eps" in p.values) == (not from_report and p.period not in (pd.Period("2019Q4"), pd.Period("2020Q1"))), e["title"]
        assert bool(p.notes and "non_ifrs_eps" in p.notes) == (p.period <= pd.Period("2016Q2")), e["title"]  # the earlier definition


@saved
def test_net_profit_over_eps_agrees_with_the_shares_except_the_known_quarters():
    off = set()
    for e, p in saved_filings():
        if not p.quarterly:
            continue
        profit, eps, shares = p.values["profit"][0], p.values["eps"][0], p.values["wa_shares"][0]
        if abs(profit / eps / shares - 1) > 0.03:
            off.add(str(p.period))
    assert off == {"2020Q3", "2024Q1"}  # EPS on the net income after a buy-out of non-controlling interests (2020Q3: 300 instead of 331)


@saved
def test_tesla_known_values():
    by_period = {p.period: p for _, p in saved_filings()}
    q = by_period[pd.Period("2026Q3")]
    assert q.values["revenue"] == [28046, 28095, 28236] and q.values["eps"] == [0.24, 0.43, 0.34] and q.values["non_ifrs_eps"] == [0.48, 0.5, 0.33]
    assert by_period[pd.Period("2020Q2")].values["non_ifrs_eps"][0] == 2.18  # before the 5-for-1 split of August 2020
    assert by_period[pd.Period("2016Q3")].values["non_ifrs_eps"][0] == 0.71
    assert by_period[pd.Period("2021Q4")].values["eps"] == [5.6, 0.74]  # the 10-K's full year
