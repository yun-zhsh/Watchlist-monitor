"""Baidu (BIDU) press releases in the `baidu_release` layout.

The snippet tests run on small tables shaped like the real ones; the saved-data tests run on the original releases in
data/filings/BIDU/ and are skipped until the scheduled workflow has saved them.
"""

import json
from functools import cache
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.baidu_release import (
    DEFINITION_2016_NOTE,
    DEFINITION_2018_NOTE,
    SUPPLIED_STATEMENTS,
    NoStatements,
    ads_ratio,
    parse_baidu_filing,
    parse_baidu_release,
)
from valuation_monitor.config import parse_config
from valuation_monitor.filings import PARSER_VERSIONS, announcements_from_frame, announcements_to_frame, build_history, parse_announcement
from valuation_monitor.market_calendar import US
from valuation_monitor.providers.base import Filing

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "BIDU"
saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved Baidu filings")


def table(*rows: list[str]) -> str:
    return "<table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"


def release(*parts: str) -> bytes:
    return ("<html><body><p>Baidu Announces Second Quarter 2026 Results</p>" + "".join(parts) + "</body></html>").encode()


CURRENT_DEFINITION = (
    "<p>Non-GAAP net income attributable to Baidu represents net income attributable to Baidu excluding share-based compensation expenses, "
    "amortization and impairment of intangible assets resulting from business combinations, disposal gain or loss, impairment of long-term "
    "investments, and fair value gain or loss of long-term investments and exchangeable bonds, adjusted for related income tax effects.</p>"
)

# ---- the layout of 2025-2026: month and day over the years, a currency row with a footnoted US$, millions, eight shares per ADS ----------

INCOME_2026 = table(
    ["Three Months Ended", "Six Months Ended"],
    ["June 30,", "March 31,", "June 30,", "June 30,", "June 30,", "June 30,", "June 30,"],
    ["2025", "2026", "2026", "2026", "2025", "2026", "2026"],
    ["RMB", "RMB", "RMB", "US$ (2)", "RMB", "RMB", "US$ (2)"],
    ["Revenue", "32,713", "32,075", "31,325", "4,617", "65,165", "63,400", "9,344"],
    ["Net income", "7,2 59", "3,291", "2,208", "326", "15,077", "5,499", "811"],
    ["Net income attributable to Baidu", "7,322", "3,445", "2,319", "342", "15,039", "5,764", "850"],
    ["Earnings per ADS (1 ADS representing 8 Class A ordinary shares):"],
    ["-Basic", "20.90", "9.38", "6.08", "0.90", "42.76", "15.46", "2.28"],
    ["-Diluted", "20.35", "8.76", "5.74", "0.85", "41.95", "14.50", "2.14"],
    ["Earnings per share for Class A and Class B ordinary shares:"],
    ["-Basic", "2.61", "1.17", "0.76", "0.11", "5.35", "1.93", "0.28"],
    ["-Diluted", "2.54", "1.10", "0.72", "0.11", "5.24", "1.81", "0.27"],
    ["Weighted average number of Class A and Class B ordinary shares outstanding (in millions):"],
    ["-Basic", "2,720", "2,721", "2,716", "2,716", "2,735", "2,719", "2,719"],
    ["-Diluted", "2,730", "2,764", "2,755", "2,755", "2,746", "2,760", "2,760"],
)
ASSETS_2026 = table(
    ["December 31,", "June 30,", "June 30,"],
    ["2025", "2026", "2026"],
    ["RMB", "RMB", "US$"],
    ["ASSETS"],
    ["Total assets", "449,157", "469,932", "69,259"],
)
EQUITY_2026 = table(  # the liabilities and equity continue in a table of their own, without a unit note
    ["December 31,", "June 30,", "June 30,"],
    ["2025", "2026", "2026"],
    ["RMB", "RMB", "US$"],
    ["Total liabilities", "159,431", "174,835", "25,767"],
    ["Equity"],
    ["Total Baidu shareholders' equity", "266,330", "271,752", "40,051"],
    ["Noncontrolling interests", "10,230", "9,660", "1,424"],
    ["Total equity", "276,560", "281,412", "41,475"],
)
RECONCILIATION_2026 = table(  # one column per segment, but the EPS rows only have one value per date
    ["Three months ended", "Three months ended", "Three months ended", "Three months ended"],
    ["June 30, 2025 (RMB)", "March 31, 2026 (RMB)", "June 30, 2026 (RMB)", "June 30, 2026 (US$)"],
    ["Baidu General Business", "iQIYI", "Baidu, Inc.", "Baidu General Business", "iQIYI", "Baidu, Inc.",
     "Baidu General Business", "iQIYI", "Baidu, Inc.", "Baidu General Business", "iQIYI", "Baidu, Inc."],
    ["Net income (loss) attributable to Baidu", "7,382", "(134", "7,322", "3,573", "(294", "3,445", "2,453", "(287", "2,319", "362", "(43", "342"],
    ["Net income (loss) attributable to Baidu (non-GAAP)", "4,792", "15", "4,795", "4,433", "(233", "4,332", "2,672", "(210", "2,573", "394", "(32", "379"],
    ["Diluted earnings per ADS", "20.35", "8.76", "5.74", "0.85"],
    ["Add: Accretion of the redeemable noncontrolling interests", "0.64", "0.74", "0.74", "0.11"],
    ["Diluted earnings per ADS (non-GAAP)", "13.58", "12.06", "7.22", "1.06"],
)
SUMMARY_2026 = table(  # the summary at the top (quarter labels, no dates): not read
    ["Baidu, Inc."],
    ["(In millions except per ADS, unaudited)", "Q2 2025", "Q1 2026", "Q2 2026", "YOY", "QOQ"],
    ["RMB", "RMB", "RMB", "US$"],
    ["Total revenues", "32,713", "32,075", "31,325", "4,617", "(4", "%)", "(2", "%)"],
    ["Diluted earnings per ADS (non-GAAP) 2", "13.58", "12.06", "7.22", "1.06", "(47", "%)", "(40", "%)"],
)
RELEASE_2026 = release(
    SUMMARY_2026,
    "<p>Baidu, Inc. Condensed Consolidated Statements of Income (In millions except for per share (or ADS) information, unaudited)</p>",
    INCOME_2026,
    "<p>Baidu, Inc. Condensed Consolidated Balance Sheets (In millions, unaudited)</p>",
    ASSETS_2026,
    EQUITY_2026,
    "<p>Reconciliations of Non-GAAP Financial Measures (In millions except for per ADS information, unaudited)</p>",
    RECONCILIATION_2026,
    CURRENT_DEFINITION,
)


def test_the_2026_layout_eight_shares_per_ads_and_the_non_gaap_eps_of_the_segment_table():
    p = parse_baidu_release(RELEASE_2026)
    assert p.period == pd.Period("2026Q2")
    assert p.currency == "CNY"
    assert p.values["revenue"] == [31325.0, 32713.0, 32075.0]  # this quarter, the same quarter last year, the previous quarter
    assert p.values["profit"] == [2319.0, 7322.0, 3445.0]
    assert p.values["eps"] == [6.08, 20.90, 9.38]  # basic, per ADS
    assert p.values["wa_shares"] == [2716 / 8, 2720 / 8, 2721 / 8]  # millions of shares / 8 shares per ADS
    assert p.values["equity"] == [271752.0, 266330.0]  # the continued table takes the unit of the one before it
    assert p.values["non_ifrs_eps"] == [7.22, 13.58, 12.06]  # diluted, as published
    assert p.notes is None  # the current definition


# ---- the layout of 2015-2016: no currency row, the years in the row of the unit note, thousands, single shares, ten ADSs per share ------

INCOME_2015 = table(
    ["Three Months Ended", "Twelve Months Ended"],
    ["December 31,", "December 31,", "September 30,", "December 31,", "December 31,"],
    ["(In RMB thousands except for share, per share (or ADS) information)", "2015", "2014", "2015", "2015", "2014"],
    ["Unaudited", "Unaudited", "Unaudited", "Unaudited", "Audited"],
    ["Revenues:"],
    ["Online marketing services", "17,610,379", "13,844,607", "17,680,374", "64,037,006", "48,495,215"],
    ["Total revenues", "18,698,792", "14,050,229", "18,383,083", "66,381,672", "49,052,318"],
    ["Net income attributable to Baidu, Inc.", "24,711,811", "3,229,069", "2,841,069", "33,664,173", "13,187,069"],
    ["Earnings per share for Class A and Class B ordinary shares:"],
    ["Net income attributable to Baidu, Inc.-Basic", "711.02", "90.51", "79.36", "958.20", "374.60"],
    ["Net income attributable to Baidu, Inc.-Diluted", "709.15", "90.06", "79.06", "956.11", "373.15"],
    ["Earnings per ADS (1 Class A ordinary share equals 10 ADSs):"],
    ["Net income attributable to Baidu, Inc.-Basic", "71.10", "9.05", "7.94", "95.82", "37.46"],
    ["Net income attributable to Baidu, Inc.-Diluted", "70.92", "9.01", "7.91", "95.61", "37.32"],
    ["Weighted average number of Class A and Class B ordinary shares outstanding:"],
    ["Basic", "34,588,703", "35,093,674", "34,849,532", "35,134,103", "35,062,466"],
    ["Diluted", "34,679,673", "35,269,030", "34,936,839", "35,208,624", "35,198,474"],
)
# the fourth-quarter 2015 release dates both balance sheet columns "December 31 2015": the audited one is the end of 2014
BALANCE_2015 = table(
    ["(In RMB thousands except for number of shares and per share data)", "December 31 2015", "December 31 2015"],
    ["Unaudited", "Audited"],
    ["ASSETS"],
    ["Total assets", "147,853,308", "99,118,062"],
    ["Total Baidu, Inc. shareholders' equity", "80,255,663", "51,072,424"],
    ["Noncontrolling interests", "4,018,325", "1,538,662"],
    ["Total equity", "84,273,988", "52,611,086"],
)
RECONCILIATION_2015 = table(
    ["Three months ended December 31, 2014", "Three months ended September 30, 2015", "Three months ended December 31, 2015"],
    ["GAAP Result", "Adjustment", "Non-GAAP Results", "GAAP Result", "Adjustment", "Non-GAAP Results", "GAAP Result", "Adjustment", "Non-GAAP Results"],
    ["Net income attributable to Baidu, Inc.", "3,229,069", "339,027", "3,568,096", "2,841,069", "400,297", "3,241,366", "24,711,811", "341,020", "25,052,831"],
)


def test_the_2015_layout_thousands_single_shares_ten_ads_per_share_and_no_non_gaap_eps():
    p = parse_baidu_release(release(INCOME_2015, BALANCE_2015, RECONCILIATION_2015))
    assert p.period == pd.Period("2015Q4")
    assert p.values["revenue"] == pytest.approx([18698.792, 14050.229, 18383.083])
    assert p.values["profit"] == pytest.approx([24711.811, 3229.069, 2841.069])
    assert p.values["eps"] == [71.10, 9.05, 7.94]
    assert p.values["wa_shares"] == pytest.approx([345.88703, 350.93674, 348.49532])  # 34.6 million shares x 10 ADSs per share
    assert p.values["equity"] == pytest.approx([80255.663, 51072.424])  # the unaudited column is this year's end
    assert "non_ifrs_eps" not in p.values  # the 2014 and 2015 releases have only a Non-GAAP net income


def test_a_balance_sheet_with_the_same_date_twice_and_no_audit_row_is_an_error():
    balance = table(
        ["(In RMB thousands except for number of shares and per share data)", "December 31 2015", "December 31 2015"],
        ["Total Baidu, Inc. shareholders' equity", "80,255,663", "51,072,424"],
    )
    with pytest.raises(ValueError, match="same date twice"):
        parse_baidu_release(release(INCOME_2015, balance))


# ---- the 2016-2018 layout: full dates without a currency row; the older Non-GAAP definitions carry a note -----------------------------

INCOME_2018 = table(
    ["Three Months Ended", "Six Months Ended"],
    ["(In RMB millions except for share, per share (or ADS) information)", "June 30, 2017", "March 31, 2018", "June 30, 2018", "June 30, 2017", "June 30, 2018"],
    ["Unaudited", "Unaudited", "Unaudited", "Unaudited", "Unaudited"],
    ["Revenues:"],
    ["Total revenues(note 1)", "20,874", "20,907", "25,972", "37,765", "46,879"],
    ["Net income attributable to Baidu", "4,415", "6,694", "6,402", "6,192", "13,096"],
    ["Earnings per share for Class A and Class B ordinary shares:"],
    ["-Basic", "113.58", "188.60", "183.16", "160.00", "371.73"],
    ["Earnings per ADS (1 Class A ordinary share equals 10 ADSs ):"],
    ["-Basic", "11.36", "18.86", "18.32", "16.00", "37.17"],
    ["Weighted average number of Class A and Class B ordinary shares outstanding:"],
    ["Basic", "34,678,268", "34,838,392", "34,953,564", "34,705,989", "34,896,297"],
)
BALANCE_2018 = table(
    ["(In RMB millions except for number of shares and per share data)", "December 31, 2017", "June 30, 2018"],
    ["Audited", "Unaudited"],
    ["Total Baidu shareholders' equity", "115,346", "149,080"],
)
RECONCILIATION_2018 = table(
    ["Three months ended"],
    ["June 30, 2017", "March 31, 2018", "June 30, 2018"],
    ["Net income attributable to Baidu", "4,415", "6,694", "6,402"],
    ["GAAP diluted earnings per ADS", "11.31", "18.68", "18.14"],
    ["Non-GAAP diluted earnings per ADS", "13.58", "13.03", "21.06"],
)
DEFINITION_2018 = (
    "<p>Non-GAAP net income attributable to Baidu represents net income attributable to Baidu excluding share-based compensation expenses, the "
    "gain or loss associated with the issuance of the shares by our equity method investees at a price higher or lower than our carrying value "
    "per share, disposal gain or loss, impairment of long-term investments and fair value change of long-term investments, as adjusted for the "
    "tax effects on non-GAAP adjustments.</p>"
)
DEFINITION_2016 = (
    "<p>Non-GAAP net income attributable to Baidu represents net income attributable to Baidu excluding share-based compensation expenses and the "
    "gain or loss associated with the issuance of the shares by our equity method investees at a price higher or lower than our carrying value "
    "per share.</p>"
)


def test_full_dates_without_a_currency_row_and_the_2018_definition_note():
    p = parse_baidu_release(release(INCOME_2018, BALANCE_2018, RECONCILIATION_2018, DEFINITION_2018))
    assert p.period == pd.Period("2018Q2")
    assert p.values["revenue"] == [25972.0, 20874.0, 20907.0]
    assert p.values["eps"] == [18.32, 11.36, 18.86]
    assert p.values["wa_shares"] == pytest.approx([349.53564, 346.78268, 348.38392])
    assert p.values["equity"] == [149080.0, 115346.0]
    assert p.values["non_ifrs_eps"] == [21.06, 13.58, 13.03]
    assert p.notes == {"non_ifrs_eps": DEFINITION_2018_NOTE}


def test_the_2016_definition_note_and_a_release_without_a_definition_is_an_error():
    p = parse_baidu_release(release(INCOME_2018, BALANCE_2018, RECONCILIATION_2018, DEFINITION_2016))
    assert p.notes == {"non_ifrs_eps": DEFINITION_2016_NOTE}
    with pytest.raises(ValueError, match="definition of the Non-GAAP net income"):
        parse_baidu_release(release(INCOME_2018, BALANCE_2018, RECONCILIATION_2018))


def test_the_definition_note_reaches_the_quarter_and_the_saved_frame():
    filing = Filing("Baidu Announces Second Quarter 2018 Results", pd.Timestamp("2018-08-06 06:10"), "u", release(INCOME_2018, BALANCE_2018, RECONCILIATION_2018, DEFINITION_2016))
    a = parse_announcement(filing, "baidu_release", US)
    assert a.series_notes == {"non_ifrs_eps": DEFINITION_2016_NOTE}
    history = build_history([a])
    assert history.quarters["non_ifrs_eps"][pd.Period("2018Q2")].note == DEFINITION_2016_NOTE
    assert "; " not in DEFINITION_2016_NOTE and "; " not in DEFINITION_2018_NOTE
    assert announcements_from_frame(announcements_to_frame([a], 1), US)[0].series_notes == a.series_notes  # kept whole when saved


# ---- what is not read, and what is an error ------------------------------------------------------------------------------------------


def test_a_release_with_only_the_summary_tables_has_no_statements():
    """The fourth-quarter 2020 release (February 19, 2021) has the summary tables only, without the statements."""
    with pytest.raises(NoStatements):
        parse_baidu_release(release(SUMMARY_2026, CURRENT_DEFINITION))


Q4_2020_URL = "https://www.sec.gov/Archives/edgar/data/1329099/000119312521049165/d102906dex991.htm"


def test_a_release_without_statements_and_without_supplied_ones_is_an_error():
    filing = Filing("Baidu Announces Fourth Quarter and Fiscal Year 2026 Results", pd.Timestamp("2027-02-18 16:05"), "https://www.sec.gov/x.htm", release(SUMMARY_2026, CURRENT_DEFINITION))
    with pytest.raises(NoStatements):  # never skipped silently: the maintainer has to supply the statements
        parse_announcement(filing, "baidu_release", US)


def test_the_fourth_quarter_of_2020_from_the_supplied_statements():
    """The abbreviated 6-K exhibit has no statements; the supplied pages of the IR release give them (the PDF is in the package)."""
    assert SUPPLIED_STATEMENTS == {Q4_2020_URL: "BIDU/2021-02-19_baidu-2020q4-ir-release-statements.pdf"}
    filing = Filing("Baidu Announces Fourth Quarter and Fiscal Year 2020 Results", pd.Timestamp("2021-02-19 17:55:35"), Q4_2020_URL, release(SUMMARY_2026, CURRENT_DEFINITION))
    a = parse_announcement(filing, "baidu_release", US)
    assert a.period == pd.Period("2020Q4") and a.currency == "CNY"
    # the same figures as the release on PR Newswire and as the previous-quarter column of the first-quarter 2021 release
    assert a.values["revenue"] == [30263.0, 28884.0, 28232.0]
    assert a.values["profit"] == [5174.0, 6345.0, 13678.0]
    assert a.values["eps"] == [15.28, 18.28, 40.21]
    assert a.values["wa_shares"] == pytest.approx([336.90743, 345.86035, 339.82547])  # 33,690,743 shares x 10 ADSs per share
    assert a.values["equity"] == [182696.0, 163599.0]
    assert a.values["non_ifrs_eps"] == [20.08, 26.54, 20.35]
    assert set(a.series_notes) == set(a.values) and all("supplied by the maintainer" in n and "; " not in n for n in a.series_notes.values())
    assert a.available == pd.Timestamp("2021-02-20")  # the 6-K's acceptance time, after the close: from the next day (trading day 2021-02-22)
    history = build_history([a])
    assert "supplied by the maintainer" in history.quarters["eps"][pd.Period("2020Q4")].note
    assert announcements_from_frame(announcements_to_frame([a], 1), US)[0].series_notes == a.series_notes


def test_the_supplied_statements_only_replace_a_release_without_statements():
    assert parse_baidu_filing(Q4_2020_URL, RELEASE_2026).period == pd.Period("2026Q2")  # a release with statements is read as it is


def test_a_row_without_one_number_per_column_is_not_read_and_a_missing_value_is_an_error():
    income = INCOME_2026.replace('<td>Revenue</td><td>32,713</td>', '<td>Revenue</td>')  # one number short
    with pytest.raises(ValueError, match=r"Not found: \['revenue'\]"):
        parse_baidu_release(RELEASE_2026.replace(INCOME_2026.encode(), income.encode()))


def test_the_same_value_in_two_tables_must_agree():
    other = RECONCILIATION_2026.replace("<td>7.22</td>", "<td>7.23</td>")
    with pytest.raises(ValueError, match="non_ifrs_eps differs between tables"):
        parse_baidu_release(RELEASE_2026.replace(RECONCILIATION_2026.encode(), (RECONCILIATION_2026 + other).encode()))


def test_amounts_without_a_unit_note_are_not_read():
    with pytest.raises(ValueError, match=r"Not found: \['equity', 'profit', 'revenue'\]"):
        parse_baidu_release(release(INCOME_2026, ASSETS_2026, EQUITY_2026, RECONCILIATION_2026, CURRENT_DEFINITION))


@pytest.mark.parametrize("eps_ads, eps_share, ratio", [([6.08, 20.90], [0.76, 2.61], 8.0), ([71.10, 9.05], [711.02, 90.51], 0.1)])
def test_ads_ratio_a_whole_number_or_its_inverse(eps_ads, eps_share, ratio):
    assert ads_ratio(eps_ads, eps_share) == pytest.approx(ratio)


def test_ads_ratio_that_is_not_whole_is_an_error():
    with pytest.raises(ValueError, match="not a whole number"):
        ads_ratio([6.08], [1.30])


def test_config_baidu():
    config = parse_config({"stocks": [{"symbol": "BIDU", "name": "Baidu (ADR)", "market": "US", "cik": 1329099, "filing_format": "baidu_release",
                                       "metrics": ["pe", "pe_non_gaap", "ps", "pb"], "eps_basis": {"pe_non_gaap": "diluted"}}]})
    stock = config.stocks[0]
    assert (stock.market, stock.filing_format, stock.cik, stock.eps_basis) == ("US", "baidu_release", 1329099, {"pe_non_gaap": "diluted"})
    assert PARSER_VERSIONS["baidu_release"] >= 1
    with pytest.raises(ValueError, match="does not match market"):
        parse_config({"stocks": [{"symbol": "0700.HK", "filing_format": "baidu_release"}]})


# ---- the saved filings (skipped until the scheduled update has saved them) ------------------------------------------------------------


@cache
def index() -> list[dict]:
    return [e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if e["results"]]


@cache
def parsed(file: str):
    e = next(x for x in index() if x["file"] == file)
    return parse_baidu_filing(e["url"], (FOLDER / file).read_bytes())


def by_period() -> dict[pd.Period, object]:
    return {parsed(e["file"]).period: parsed(e["file"]) for e in index()}


@saved
def test_every_saved_release_parses_and_no_quarter_is_missing():
    periods = [parsed(e["file"]).period for e in index()]
    assert periods == sorted(periods) and len(set(periods)) == len(periods)
    assert set(pd.period_range(periods[0], periods[-1], freq="Q")) == set(periods)  # the fourth quarter of 2020 from the supplied statements
    assert [e["file"] for e in index() if parsed(e["file"]).notes and "supplied" in parsed(e["file"]).notes.get("eps", "")] == ["2021-02-19_d102906dex991.htm"]


@saved
def test_net_profit_over_eps_agrees_with_the_ads_except_where_the_accretion_weighs():
    """Baidu's basic EPS is on the net income attributable to Baidu less the accretion of its subsidiaries' redeemable noncontrolling interests
    (and its share of the subsidiaries' dilution), so net income / EPS is a few percent above the ADSs; more where EPS is small."""
    for period, p in by_period().items():
        profit, eps, ads = p.values["profit"][0], p.values["eps"][0], p.values["wa_shares"][0]
        if abs(eps) < 5:  # 2020Q1 (0.05), 2022Q3 (-0.87), 2025Q4 (4.48), 2026Q3 (4.58): the accretion is a large part of EPS
            continue
        assert abs(profit / eps / ads - 1) < 0.13, (period, profit, eps, ads)


@saved
def test_comparison_columns_match_the_first_published_values():
    first: dict[tuple[str, pd.Period], float] = {}
    differ = set()
    for period, p in sorted(by_period().items()):
        for key, vals in p.values.items():
            periods = [period, pd.Period(year=period.year - 1, quarter=4, freq="Q")] if key == "equity" else [period, period - 4, period - 1]
            for q, v in zip(periods, vals):
                f = first.setdefault((key, q), v)
                tol = 0.011 if key in ("eps", "non_ifrs_eps") else max(1.5, abs(f) * 0.002)
                if abs(f - v) > tol:
                    differ.add((key, str(q)))
    # 2014: restated in the fourth-quarter 2015 release; Non-GAAP EPS: recast under the definitions of the first quarters of 2018 and 2019
    assert differ == {("eps", "2014Q4"), ("profit", "2014Q4"), ("equity", "2014Q4"),
                      *{("non_ifrs_eps", q) for q in ("2017Q2", "2017Q3", "2017Q4", "2018Q1", "2018Q2", "2018Q3", "2018Q4")}}


@saved
def test_baidu_known_values():
    p = by_period()
    assert p[pd.Period("2026Q2")].values["eps"][0] == 6.08
    assert p[pd.Period("2026Q2")].values["non_ifrs_eps"][0] == 7.22
    assert p[pd.Period("2015Q4")].values["equity"] == pytest.approx([80255.663, 51072.424])
    assert p[pd.Period("2021Q1")].values["eps"] == [76.10, 0.05, 15.28]  # the previous-quarter column gives the fourth quarter of 2020
    assert p[pd.Period("2017Q4")].notes == {"non_ifrs_eps": DEFINITION_2016_NOTE}
    assert p[pd.Period("2018Q4")].notes == {"non_ifrs_eps": DEFINITION_2018_NOTE}
    assert p[pd.Period("2019Q1")].notes is None
