"""Advantech (2395.TW, Taiwan): the IR website provider and the Taiwan-IFRS statement parser (tifrs_statements), on text fragments and on the
excerpts of the English consolidated financial statements saved in the repo (data/filings/2395.TW)."""

import io
import json
from datetime import datetime
from functools import cache
from pathlib import Path

import pandas as pd
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from valuation_monitor.config import DEFAULT_MAX_PRICE_AGE_DAYS, parse_config
from valuation_monitor.market_calendar import get_market
from valuation_monitor.providers.advantech import AdvantechIrProvider, discover
from valuation_monitor.tifrs import MisprintedAmount, amounts_after, approval, page_texts, parse_tifrs, statement_pages, INCOME_HEADING

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "2395.TW"


def text_pdf(pages: list[list[str]]) -> bytes:
    """A PDF whose pages show these lines of text in Helvetica (one of the standard fonts, so nothing is embedded)."""
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                                NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for lines in pages:
        page = writer.add_blank_page(600, 800)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        content = StreamObject()
        ops = [f"BT /F1 8 Tf 20 {780 - 12 * i} Td ({line.replace('(', chr(92) + '(').replace(')', chr(92) + ')')}) Tj ET" for i, line in enumerate(lines)]
        content._data = "\n".join(ops).encode("latin-1")
        page[NameObject("/Contents")] = writer._add_object(content)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# --- publication time: the board's approval sentence ---


@pytest.mark.parametrize(
    "sentence, period_end, day",
    [
        ("The consolidated financial statements were approved by the board of directors and authorized for issue on May 3, 2013.", "2013-03-31", "2013-05-03"),
        ("The consolidated financial statements were approved by the board of directors April 29, 2016.", "2016-03-31", "2016-04-29"),
        ("The consolidated financial statements were approved by the Company’s board of directors on August 2, 2019.", "2019-06-30", "2019-08-02"),
        ("These consolidated financial statements were authorised for issuance by the Board of Directors on October 29, 2026.", "2026-09-30", "2026-10-29"),
    ],
)
def test_approval_sentences_of_both_auditors(sentence, period_end, day):
    assert approval(["Notes", sentence], pd.Timestamp(period_end)) == (pd.Timestamp(day), 1)


def test_approval_of_an_earlier_resolution_is_not_the_publication_day():
    texts = [
        "The cash dividends were approved by the board of directors on February 27, 2025.",  # a dividend of the year before: outside 15-100 days
        "These consolidated financial statements were authorised for issuance by the Board of Directors on February 26, 2026.",
    ]
    assert approval(texts, pd.Timestamp("2025-12-31")) == (pd.Timestamp("2026-02-26"), 1)
    assert approval(texts[:1], pd.Timestamp("2025-12-31")) is None


# --- the IR website pages ---

BASE = "https://advcloudfiles.advantech.com/investor/"
PAGE = " ".join(
    f'<a href="{BASE}{path}">PDF</a>'
    for path in [
        "Quarterly/2013_1Q_Consolidated-English.pdf",
        "Quarterly/2026_Q3_Consolidated-English.pdf",
        "Annual/2025_Annual_Consolidated_English.pdf",
        "Shareholder-Annual-Report/106/1-en.pdf",
        "Shareholder-Annual-Report/115/1-en.pdf",
        "Events/Advantech_3Q_2026_Investors_Meeting_English.pdf",
    ]
)


def test_discover_reads_the_three_kinds_and_prefers_the_annual_statements():
    found = discover([PAGE])
    assert found[(2013, 1)] == ("quarterly", BASE + "Quarterly/2013_1Q_Consolidated-English.pdf")  # 2013 names the quarter "1Q"
    assert found[(2026, 3)][0] == "quarterly"
    assert found[(2016, 4)] == ("annual_report", BASE + "Shareholder-Annual-Report/106/1-en.pdf")  # ROC year 106 = 2017, covering 2016
    assert found[(2025, 4)] == ("annual", BASE + "Annual/2025_Annual_Consolidated_English.pdf")  # not the shareholder annual report of ROC 115
    assert len(found) == 4  # the investor meeting deck is not a report


def report_pdf(year: int, quarter: int, approved: str | None) -> bytes:
    end = pd.Period(year=year, quarter=quarter, freq="Q").end_time
    heading = "ADVANTECH CO., LTD. AND SUBSIDIARIES CONSOLIDATED"
    pages = [
        ["ADVANTECH CO., LTD. AND SUBSIDIARIES", "CONSOLIDATED FINANCIAL STATEMENTS"],
        [f"{heading} BALANCE SHEETS", f"{end:%B} {end.day}, {year} December 31, {year - 1}",
         "31XX Equity attributable to shareholders of the parent 62,660,962 63 54,947,481 73"],
        [f"{heading} STATEMENTS OF COMPREHENSIVE INCOME", f"For the three-month periods ended {end:%B} {end.day}, {year} {year - 1}",
         "4000 Operating revenue 6(21) and 7 $ 32,440,238 100 $ 17,773,911 100",
         "Profit (loss) attributable to: 8610 Shareholders of the parent $ 6,217,878 19 $ 2,766,103 16",
         "Basic earnings per share (in dollars) 6(27) 9750 Profit for the period $ 7.15 $ 3.20"],
        ["NOTES TO THE CONSOLIDATED FINANCIAL STATEMENTS"]
        + ([f"These consolidated financial statements were authorised for issuance by the Board of Directors on {approved}."] if approved else []),
        ["(27) Earnings per share", "Weighted average number of ordinary shares used in the computation of basic earnings per share 869,122 864,558"],
        ["A table that may continue the note"],
        ["Unrelated note"] * 3,
    ]
    return text_pdf(pages)


class FakeHttp:
    def __init__(self, reports: dict[str, bytes]):
        self.reports = reports
        self.requests = []

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        if "advantech.com/en/investor" in url:
            return " ".join(f'<a href="{u}">' for u in self.reports).encode()
        return self.reports[url]


def test_provider_dates_reports_by_the_board_approval_and_saves_excerpts(tmp_path):
    q3 = BASE + "Quarterly/2026_Q3_Consolidated-English.pdf"
    q2 = BASE + "Quarterly/2026_Q2_Consolidated-English.pdf"
    fy = BASE + "Shareholder-Annual-Report/106/1-en.pdf"
    http = FakeHttp({q3: report_pdf(2026, 3, "October 29, 2026"), q2: report_pdf(2026, 2, None), fy: report_pdf(2016, 4, "March 6, 2017")})
    provider = AdvantechIrProvider(http, tmp_path)
    found = provider.results_announcements("2395.TW", datetime(2015, 1, 1))
    assert [(f.title, f.published) for f in found] == [
        ("Advantech 2016 full year shareholder annual report", pd.Timestamp("2017-03-06 13:30")),
        ("Advantech 2026 Q3 consolidated financial statements", pd.Timestamp("2026-10-29 13:30")),  # 2026Q2 states no approval day: skipped
    ]
    assert "after the 13:30 close" in found[1].note and "shareholder annual report" in found[0].note
    assert get_market("TW").available_date(found[1].published) == pd.Timestamp("2026-10-30")  # the next day
    index = json.loads((tmp_path / "2395.TW" / "index.json").read_text(encoding="utf-8"))
    assert [(e["year"], e["quarter"], e["kind"]) for e in index] == [(2016, 4, "annual_report"), (2026, 3, "quarterly")]
    saved = tmp_path / "2395.TW" / index[1]["file"]
    assert index[1]["file"] == "2026Q3_2026_Q3_Consolidated-English.pdf"
    assert len(PdfReader(saved).pages) == 6  # the unrelated last page is not kept
    p = parse_tifrs(found[1].title, saved.read_bytes())
    assert p.values["eps"] == [7.15, 3.2] and p.values["wa_shares"] == [869.122, 864.558]
    # the FY2016 figures came from the later shareholder annual report: public from the approval, the report itself by the end of June
    release = provider.release_info("2395.TW")[fy]
    assert release.first_published == pd.Timestamp("2017-03-06 13:30") and release.first_url is None
    assert release.public_by == pd.Timestamp("2017-06-30")
    assert q3 not in provider.release_info("2395.TW")
    # the saved reports are used without downloading them again (the one without an approval day is checked again)
    http.requests.clear()
    assert len(provider.results_announcements("2395.TW", datetime(2015, 1, 1))) == 2
    assert q3 not in http.requests and fy not in http.requests and q2 in http.requests


def test_saved_reports_are_used_when_the_website_cannot_be_reached(tmp_path):
    q3 = BASE + "Quarterly/2026_Q3_Consolidated-English.pdf"
    AdvantechIrProvider(FakeHttp({q3: report_pdf(2026, 3, "October 29, 2026")}), tmp_path).results_announcements("2395.TW", datetime(2015, 1, 1))

    class Down:
        def get_bytes(self, url, **kwargs):
            raise ConnectionError("offline")

    assert len(AdvantechIrProvider(Down(), tmp_path).results_announcements("2395.TW", datetime(2015, 1, 1))) == 1


# --- parsing ---

DELOITTE_INCOME = (
    "- 4 - ADVANTECH CO., LTD. AND SUBSIDIARIES CO NSOLIDATED STATEMENTS O F COMPREHENSIVE INCOME (In Thousands of New Taiwan Dollars, Except "
    "Earnings Per Share) For the Three Months Ended September 30 For the Nine Months Ended September 30 2015 (Reviewed) 2014 (Reviewed after Restated) "
    "OPERATING REVENUE (Note 27) Sales $ 9,197,608 97 $ 8,933,587 98 $ 27,531,015 97 $ 25,874,022 97 Other operating revenue 240,144 3 190,950 2 "
    "718,351 3 675,080 3 Total operating revenue 9,437,752 100 9,124,537 100 28,249,366 100 26,549,102 100 OPERATING COSTS (Notes 11, 21 and 27)"
)
DELOITTE_INCOME_2 = (
    "- 5 - ADVANTECH CO., LTD. AND SUBSIDIARIES CO NSOLIDATED STATEMENTS O F COMPREHENSIVE INCOME For the Three Months Ended September 30 "
    "TOTAL COMPREHENSIVE INCOME FOR THE PERIOD $ 1,340,725 14 $ 1,147,638 13 $ 3,443,137 12 $ 3,930,277 15 NET PROFIT ATTRIBUTABLE TO: "
    "Owners of the Company $ 1,228,025 13 $ 1,319,749 15 $ 3,762,812 13 $ 3,674,203 14 Non-controlling interests 7,893 - 7,393 - 12,011 - 17,643 - "
    "TOTAL COMPREHENSIVE INCOME ATTRIBUTABLE TO: Owners of the Company $ 1,335,846 14 $ 1,143,235 13 $ 3,448,129 12 $ 3,918,967 15 "
    "EARNINGS PER SHARE (Note 23) Basic $ 1.94 $ 2.10 $ 5.96 $ 5.85 Diluted $ 1.94 $ 2.09 $ 5.93 $ 5.82"
)
DELOITTE_BALANCE = (
    "- 3 - ADVANTECH CO., LTD. AND SUBSIDIARIES CONSOLIDATED BALANCE SHEETS (In Thousands of New Taiwan Dollars) September 30, 2015 (Reviewed) "
    "December 31, 2014 (Audited after Restated) September 30, 2014 EQUITY ATTRIBUTABLE TO OWNERS OF THE COMPANY Share capital Ordinary shares "
    "6,318,531 20 6,301,031 20 6,287,021 21 Total equity attributable to owners of the Company 22,163,176 70 22,346,019 71 20,415,117 69 "
    "NON-CONTROLLING INTERESTS 137,791 - 187,000 - 187,503 1"
)
DELOITTE_NOTE = (
    "Weighted Average Number of Ordinary Shares Outstanding (In Thousand Shares) For the Three Months Ended September 30 For the Nine Months "
    "Ended September 30 2015 2014 2015 2014 Weighted average number of ordinary shares in computation of basic earnings per share 631,663 629,081 "
    "631,559 628,232 Effect of dilutive potential ordinary shares"
)
PARENT_ONLY = "ADVANTECH CO., LTD. STATEMENTS OF COMPREHENSIVE INCOME FOR THE YEARS ENDED DECEMBER 31, 2016 AND 2015 EARNINGS PER SHARE Basic $8.96 $8.08"


def test_deloitte_layout_takes_the_three_month_columns():
    texts = ["cover", "report", "", DELOITTE_BALANCE, DELOITTE_INCOME, DELOITTE_INCOME_2, "notes", DELOITTE_NOTE]
    p = parse_tifrs("Advantech 2015 Q3 consolidated financial statements", b"", texts)
    assert p.period == pd.Period("2015Q3") and p.quarterly and p.currency == "TWD"
    assert p.values["revenue"] == [9437.752, 9124.537]  # thousands of TWD -> millions
    assert p.values["profit"] == [1228.025, 1319.749]  # not the total comprehensive income attributable
    assert p.values["eps"] == [1.94, 2.10]
    assert p.values["equity"] == [22163.176, 22346.019]  # this period end and December 31 of last year
    assert p.values["wa_shares"] == [631.663, 629.081]
    assert p.notes is None


def test_statement_headings_ignore_the_parent_company_only_statements():
    texts = [PARENT_ONLY, "other", DELOITTE_INCOME, DELOITTE_INCOME_2, "notes"]
    assert statement_pages(texts, INCOME_HEADING) == [2, 3]  # "CO NSOLIDATED STATEMENTS O F" as the text extraction splits it


PWC_BALANCE = (
    "ADVANTECH CO., LTD. AND SUBSIDIARIES CONSOLIDATED BALANCE SHEETS JUNE 30, 2026, DECEMBER 31, 2025 AND JUNE 30, 2025 Equity attributable to "
    "shareholders of the parent Share capital 6(16) 3110 Common shares 8,690,460 9 8,651,898 12 8,643,744 12 3400 Other equity 3,524,076 3 1,069,064 1 "
    "1XX Equity attributable to shareholders of the parent 55,335,278 63 54,947,481 73 51,351,792 74 36XX Non-controlling interest 99,671 - 101,784 -"
)
PWC_INCOME = (
    "ADVANTECH CO., LTD. AND SUBSIDIARIES CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME FOR THE SIX-MONTH PERIODS ENDED JUNE 30, 2026 AND 2025 "
    "4000 Operating revenue 6(20) and 7 $ 26,26,290 100 $ 17,835,796 100 $ 46,511,554 100 $ 35,187,042 100 5000 Operating costs "
    "Profit (loss) attributable to: 8610 Shareholders of the parent $ 4,517,872 18 $ 1,988,963 11 $ 7,852,037 17 $ 4,722,963 13 "
    "8620 Non-controlling interest ( 4,180 ) - 15,850 - Basic earnings per share (in dollars) 6(27) 9750 Profit for the period $ 5.20 $ 2.30 $ 9.05 $ 5.47"
)
PWC_NOTES = (
    "(21) Operating revenue A. Revenue from contracts with customers is recognised when control transfers. For the three-month periods ended June 30, "
    "2026 2025 Revenue from contracts with customers 26,126,290$ 17,835,796$ 2026 2025 Revenue from contracts with customers 46,511,554$ 35,187,042$ "
    "Weighted average number of ordinary shares used in the computation of basic earnings per share 868,254 864,167"
)


def test_a_misprinted_revenue_is_refused_and_read_from_the_revenue_note():
    p = parse_tifrs("Advantech 2026 Q2 consolidated financial statements", b"", ["cover", PWC_BALANCE, PWC_INCOME, PWC_NOTES])
    assert p.values["revenue"] == [26126.29, 17835.796]  # not 17,835,796 as the first well-formed amount after the label
    assert "'26,26,290'" in p.notes["revenue"] and "revenue note" in p.notes["revenue"]
    assert p.values["profit"] == [4517.872, 1988.963] and p.values["eps"] == [5.2, 2.3]
    assert p.values["equity"] == [55335.278, 54947.481]  # the total row (its code cut to "1XX"), not the heading of the section
    assert p.values["wa_shares"] == [868.254, 864.167]


def test_amount_tokens():
    assert amounts_after("8620 Non-controlling interest ( 4,180 ) - 15,850 -", 0, 2, "x") == [-4180, 15850]
    assert amounts_after("Revenue from contracts with customers 26,126,290$ 17,835,796$", 0, 2, "x") == [26126290, 17835796]
    assert amounts_after("Notes 6(21) and 7 $ 1,234 100 $ 999,999 100", 0, 2, "x") == [1234, 999999]  # note numbers and percentages are skipped
    with pytest.raises(MisprintedAmount):
        amounts_after("$ 26,26,290 100 $ 17,835,796 100", 0, 2, "Operating revenue")
    with pytest.raises(ValueError, match="fewer than 2"):
        amounts_after("Operating revenue 1,234 100", 0, 2, "Operating revenue")


def test_a_report_for_another_period_is_rejected():
    with pytest.raises(ValueError, match="not for the period ended September 30"):
        parse_tifrs("Advantech 2026 Q3 consolidated financial statements", b"", ["cover", PWC_BALANCE, PWC_INCOME, PWC_NOTES])


def test_shares_far_from_profit_over_eps_are_not_used(capsys):
    notes = PWC_NOTES.replace("868,254 864,167", "6,254 4,167")
    p = parse_tifrs("Advantech 2026 Q2 consolidated financial statements", b"", ["cover", PWC_BALANCE, PWC_INCOME, notes])
    assert "wa_shares" not in p.values and "shares are implied instead" in capsys.readouterr().err


def test_the_parser_reads_a_pdf():
    pdf = report_pdf(2026, 3, "October 29, 2026")
    assert "Board of Directors on October 29, 2026" in page_texts(pdf)[3]
    p = parse_tifrs("Advantech 2026 Q3 consolidated financial statements", pdf)
    assert p.values["revenue"] == [32440.238, 17773.911] and p.values["equity"] == [62660.962, 54947.481]


# --- market and configuration ---


def test_taiwan_market_and_config_defaults():
    stock = parse_config({"stocks": [{"symbol": "2395.TW", "name": "Advantech", "metrics": ["pe", "ps", "pb"]}]}).stocks[0]
    assert (stock.market, stock.filing_format, stock.filing_source) == ("TW", "tifrs_statements", "Advantech IR website (English financial statements)")
    assert parse_config({"stocks": [{"symbol": "6488.TWO", "name": "A Taipei Exchange stock"}]}).stocks[0].market == "TW"
    with pytest.raises(ValueError, match="does not match market TW"):
        parse_config({"stocks": [{"symbol": "2395.TW", "filing_format": "kifrs_statements"}]})
    with pytest.raises(ValueError, match="does not match market"):
        parse_config({"stocks": [{"symbol": "0700.HK", "filing_format": "tifrs_statements"}]})
    tw = get_market("TW")
    assert tw.timezone == "Asia/Taipei"
    assert tw.available_date(pd.Timestamp("2026-10-29 13:29")) == pd.Timestamp("2026-10-29")
    assert tw.available_date(pd.Timestamp("2026-10-29 13:30")) == pd.Timestamp("2026-10-30")
    assert not tw.is_half_day(pd.Timestamp("2026-12-31"))
    assert DEFAULT_MAX_PRICE_AGE_DAYS["TW"] == 12  # the Lunar New Year closure can last nine days


# --- the saved reports ---

saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved Advantech statements")


@cache
def index() -> list[dict]:
    return json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))


def title(e: dict) -> str:
    return f"Advantech {e['year']} {'full year' if e['quarter'] == 4 else 'Q%d' % e['quarter']} consolidated financial statements"


@cache
def parsed(year: int, quarter: int):
    e = next(e for e in index() if (e["year"], e["quarter"]) == (year, quarter))
    return parse_tifrs(title(e), (FOLDER / e["file"]).read_bytes())


@saved
def test_every_saved_report_parses_and_its_shares_agree_with_profit_over_eps():
    assert len(index()) >= 47 and (index()[0]["year"], index()[0]["quarter"]) == (2015, 1)
    for e in index():
        p = parsed(e["year"], e["quarter"])
        assert p.period == pd.Period(year=e["year"], quarter=e["quarter"], freq="Q") and p.quarterly == (e["quarter"] != 4)
        assert {"revenue", "profit", "eps", "equity", "wa_shares"} <= p.values.keys()
        implied = p.values["profit"][0] / p.values["eps"][0]
        assert abs(p.values["wa_shares"][0] / implied - 1) < 0.005, e["file"]
        # the publication day is the board approval stated in the saved excerpt
        end = pd.Period(year=e["year"], quarter=e["quarter"], freq="Q").end_time
        assert approval(page_texts((FOLDER / e["file"]).read_bytes()), end)[0] == pd.Timestamp(e["published"][:10])
        assert e["published"].endswith("T13:30:00")


@saved
def test_saved_quarter_annual_statements_and_shareholder_annual_report():
    q3 = parsed(2026, 3)
    assert q3.values["revenue"] == [32440.238, 17773.911] and q3.values["eps"] == [7.15, 3.2]
    fy = parsed(2025, 4)
    assert not fy.quarterly and fy.values["eps"] == [12.25, 10.45] and fy.values["equity"] == [54947.481, 51428.352]
    report = parsed(2016, 4)  # the consolidated statements of the shareholder annual report, not its parent-company-only ones
    assert report.values["profit"] == [5666.862, 5104.346] and report.values["eps"] == [8.96, 8.08]
    assert parsed(2026, 2).values["revenue"][0] == 26126.29 and "revenue note" in parsed(2026, 2).notes["revenue"]


# Yahoo's split records: the 10% stock dividends (ex-rights day -> ratio)
BONUS = {"2014-09-01": 1.099433, "2017-06-30": 1.099964, "2020-07-31": 1.099863, "2023-07-04": 1.0998772}
# Prior-year columns that differ from the figure published a year earlier: restatements in the later report (2016Q1 profit 1,312 -> 1,357
# million; 2016Q2-Q3, 2018Q3, 2019Q1 and 2019Q3 by 0.1-0.3%), the company's own rounding of restated EPS (2022Q3 4.10 / 1.1 shown as 3.72,
# 2023Q1), and 2019Q2 in the 2020Q2 report: that report was approved on the ex-rights day of the 2020 stock dividend, before the bonus shares
# were issued, so neither its own nor its prior-year EPS is restated yet
DIFFERENT = {(2016, 1), (2016, 2), (2016, 3), (2018, 3), (2019, 1), (2019, 2), (2019, 3), (2022, 3), (2023, 1)}


@saved
def test_prior_year_columns_repeat_the_figures_a_year_earlier():
    published = {(e["year"], e["quarter"]): e["published"][:10] for e in index()}
    checked = 0
    for (year, quarter), day in published.items():
        if quarter == 4 or (year - 1, quarter) not in published or (year - 1, quarter) in DIFFERENT:
            continue
        now, before = parsed(year, quarter), parsed(year - 1, quarter)
        # a stock dividend between the two restates the prior-year EPS (one on the earlier report's own day was not reflected in it)
        factor = 1.0
        for split_day, ratio in BONUS.items():
            if published[(year - 1, quarter)] <= split_day <= day:
                factor *= ratio
        assert now.values["revenue"][1] == before.values["revenue"][0], (year, quarter)
        assert now.values["profit"][1] == before.values["profit"][0], (year, quarter)
        assert abs(now.values["eps"][1] * factor - before.values["eps"][0]) <= 0.011, (year, quarter, now.values["eps"], before.values["eps"])
        checked += 1
    assert checked >= 20


# --- the split rule for a filing approved on the ex-rights day (filings.shares_before_split) ---

SPLITS = pd.Series({pd.Timestamp(day): ratio for day, ratio in BONUS.items()})


def announcement(period: str, published: str, eps: list[float], profit: list[float], shares: list[float]):
    from valuation_monitor.filings import Announcement

    return Announcement(pd.Period(period), pd.Timestamp(published), f"https://example/{period}.pdf", "TWD", True,
                        {"eps": eps, "profit": profit, "revenue": [1.0, 1.0], "wa_shares": shares}, "TW")


def test_a_filing_approved_on_the_ex_rights_day_is_on_the_old_share_basis():
    from valuation_monitor.filings import adjust_for_splits

    q2_2019 = announcement("2019Q2", "2019-08-02 13:30", [2.71, 2.27], [1894.118, 1584.195], [699.076, 697.49])
    q1 = announcement("2020Q1", "2020-04-29 13:30", [1.85, 2.31], [1294.665, 1613.136], [700.573, 698.744])
    q2 = announcement("2020Q2", "2020-07-31 13:30", [3.29, 2.71], [2307.6, 1892.289], [700.87, 699.076])  # the 2020 split is dated 2020-07-31
    q3 = announcement("2020Q3", "2020-10-30 13:30", [2.52, 2.78], [1941.395, 2136.117], [771.537, 769.212])  # restated for the bonus shares
    out = {a.period: a for a in adjust_for_splits([q2_2019, q1, q2, q3], SPLITS)}
    after_2020 = 1.0998772  # only the 2023 split is after the 2020Q2 report
    both = 1.099863 * after_2020
    assert out[pd.Period("2020Q2")].values["eps"][0] == pytest.approx(3.29 / both)  # 2.7197, as the 2021Q2 report restates it (2.99 / 1.0999)
    assert out[pd.Period("2020Q2")].values["wa_shares"][0] == pytest.approx(700.87 * both)  # 847.9 million, not 770.9
    assert out[pd.Period("2020Q2")].values["eps"][1] == pytest.approx(2.71 / both)  # the prior-year EPS is on the same old basis
    assert out[pd.Period("2020Q2")].per_share_factors == (pytest.approx(both), pytest.approx(both))
    note = out[pd.Period("2020Q2")].note
    assert note.startswith("Split basis: the filing's EPS and weighted average shares (700.9 million) are on the shares before the split")
    assert "847.5 million in the previous filing" in note and "repeats the earlier filing" not in note
    # the usual factor already fits the restated report of the next quarter: the rule does not fire
    assert out[pd.Period("2020Q3")].values["wa_shares"][0] == pytest.approx(771.537 * after_2020)
    assert out[pd.Period("2020Q3")].values["eps"][0] == pytest.approx(2.52 / after_2020) and "Split basis" not in out[pd.Period("2020Q3")].note
    # and nothing changes without a split in the window
    assert out[pd.Period("2020Q1")].values["wa_shares"][0] == pytest.approx(700.573 * both) and out[pd.Period("2020Q1")].note == ""


def test_the_split_rule_needs_a_share_count_that_fits_another_split_factor():
    from valuation_monitor.filings import adjust_for_splits

    q1 = announcement("2020Q1", "2020-04-29 13:30", [1.85, 2.31], [1294.665, 1613.136], [700.573, 698.744])
    # the share count jumps (a large issue of new shares, say) but no split factor explains it: left as it is
    odd = announcement("2020Q2", "2020-07-31 13:30", [3.205, 2.71], [2307.6, 1892.289], [720.0, 699.076])  # 6.6% below on the usual basis, 2.8% above with the other factor
    out = adjust_for_splits([q1, odd], SPLITS)
    assert out[1].values["wa_shares"][0] == pytest.approx(720.0 * 1.0998772) and "before the split" not in out[1].note


# --- encrypted reports, and a report that fails among others ---


def encrypted(pdf: bytes, user_password: str = "") -> bytes:
    """The PDF encrypted with AES-128, as Advantech's 2017 shareholder annual report is (an empty user password opens it)."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    writer.encrypt(user_password=user_password, owner_password="owner", algorithm="AES-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_the_declared_dependencies_decrypt_an_aes_encrypted_report():
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pypdf[crypto]' in pyproject.split("[project.optional-dependencies]")[0]  # a base dependency: the scheduled workflow installs no extras
    pdf = encrypted(report_pdf(2017, 3, "October 31, 2017"))
    assert PdfReader(io.BytesIO(pdf)).is_encrypted
    assert "Board of Directors on October 31, 2017" in page_texts(pdf)[3]
    assert parse_tifrs("Advantech 2017 Q3 consolidated financial statements", pdf).values["eps"] == [7.15, 3.2]


def test_an_encrypted_report_is_saved_as_an_unencrypted_excerpt(tmp_path):
    url = BASE + "Shareholder-Annual-Report/107/1-en.pdf"  # ROC year 107 (2018) covers 2017
    found = AdvantechIrProvider(FakeHttp({url: encrypted(report_pdf(2017, 4, "March 2, 2018"))}), tmp_path).results_announcements("2395.TW", datetime(2015, 1, 1))
    assert [f.published for f in found] == [pd.Timestamp("2018-03-02 13:30")]
    saved = PdfReader(io.BytesIO(found[0].content))
    assert not saved.is_encrypted and len(saved.pages) == 6


def test_a_report_that_cannot_be_read_does_not_lose_the_others(tmp_path, capsys):
    q1, q2, q3 = (BASE + f"Quarterly/2017_Q{q}_Consolidated-English.pdf" for q in (1, 2, 3))
    http = FakeHttp({
        q1: report_pdf(2017, 1, "April 28, 2017"),
        q2: encrypted(report_pdf(2017, 2, "July 27, 2017"), user_password="secret"),  # cannot be opened without the password
        q3: report_pdf(2017, 3, "October 31, 2017"),
    })
    found = AdvantechIrProvider(http, tmp_path).results_announcements("2395.TW", datetime(2015, 1, 1))
    assert [f.url for f in found] == [q1, q3]  # the reports before and after the failing one are kept
    err = capsys.readouterr().err
    assert f"ERROR 2395.TW: 2017Q2 ({q2}) cannot be read" in err and "tried again on the next update" in err
    index = json.loads((tmp_path / "2395.TW" / "index.json").read_text(encoding="utf-8"))
    assert [(e["year"], e["quarter"]) for e in index] == [(2017, 1), (2017, 3)]  # recorded, so the next run does not fetch them again
    # the next run tries only the failing report again; once it can be read it is added
    http.reports[q2] = report_pdf(2017, 2, "July 27, 2017")
    http.requests.clear()
    found = AdvantechIrProvider(http, tmp_path).results_announcements("2395.TW", datetime(2015, 1, 1))
    assert [f.url for f in found] == [q1, q2, q3] and q1 not in http.requests and q3 not in http.requests


def test_reports_fetched_before_the_website_fails_are_recorded(tmp_path):
    q1, q2 = (BASE + f"Quarterly/2017_Q{q}_Consolidated-English.pdf" for q in (1, 2))

    class FailsAfterTheFirstReport(FakeHttp):
        def get_bytes(self, url, **kwargs):
            if url == q2:
                raise ConnectionError("the website went down")
            return super().get_bytes(url, **kwargs)

    http = FailsAfterTheFirstReport({q1: report_pdf(2017, 1, "April 28, 2017"), q2: b""})
    assert [f.url for f in AdvantechIrProvider(http, tmp_path).results_announcements("2395.TW", datetime(2015, 1, 1))] == [q1]
    assert len(json.loads((tmp_path / "2395.TW" / "index.json").read_text(encoding="utf-8"))) == 1
