"""SK Hynix (000660.KS): the IR website provider (providers/skhynix.py) and the skhynix_statements layout (skhynix.py), on text fragments of the
review reports and earnings presentations, and on the excerpts saved in the repo (data/filings/000660.KS)."""

import io
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from valuation_monitor.config import parse_config
from valuation_monitor.filings import Announcement, PARSER_VERSIONS, parse_announcement
from valuation_monitor.market_calendar import KR, get_market
from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.skhynix import (
    API,
    NOTES,
    SkHynixIrProvider,
    parse_calls,
    parse_reports,
    publication,
    read_call_times,
    report_period,
)
from valuation_monitor.skhynix import deck_pages, parse_deck_texts, parse_report_texts, statement_pages

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "000660.KS"
CDN = "https://cdn.example/web"


def text_pdf(pages: list[list[str]]) -> bytes:
    """A PDF whose pages show these lines of text in Helvetica (a standard font, so nothing is embedded); an empty page has no text, like a scan."""
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


# --- review and audit reports ---

HEAD = "SK hynix Inc. and Subsidiaries"
BS_2026Q2 = [
    f"{HEAD} Consolidated Interim Statements of Financial Position June 30, 2026 and December 31, 2025 (Unaudited) 3 (In millions of Korean won) "
    "Notes June 30, 2026 December 31, 2025 Assets Current assets Cash and cash equivalents 5,6 W 26,835,986 W 14,923,766",
    f"{HEAD} Consolidated Interim Statements of Financial Position, Continued June 30, 2026 and December 31, 2025 (Unaudited) 4 "
    "(In millions of Korean won) Total liabilities 86,168,986 55,440,908 Equity Equity attributable to owners of the Parent Company "
    "Capital stock 19 3,657,652 3,657,652 Total equity attributable to owners of the Parent Company 262,380,610 120,516,178 "
    "Non-controlling interests 312,618 150,573 Total equity 262,693,228 120,666,751 Total liabilities and equity W 348,862,214 W 176,107,659",
]
IS_2026Q2 = (
    f"{HEAD} Consolidated Interim Statements of Comprehensive Income Three-month and Six-month periods ended June 30, 2026 and 2025 (Unaudited) 5 "
    "(In millions of Korean won, except per share information) Period Ended June 30 2026 2025 Notes Three months Six months Three months Six months "
    "Revenue 4,21,28 W 79,318,746 W 131,895,033 W 22,231,952 W 39,871,093 Cost of sales 23,28 13,327,390 24,224,263 10,248,628 17,785,778 "
    "Profit for the period W 93,922,593 W 134,268,502 W 6,996,216 W 15,104,411 Profit attributable to: Owners of the Parent Company "
    "W 93,820,236 W 134,150,412 W 6,997,228 W 15,104,309 Non-controlling interests 102,357 118,090 (1,012) 102 Total comprehensive income "
    "attributable to: Owners of the Parent Company W 94,864,501 W 136,218,979 W 5,943,034 W 14,057,088 Earnings per share 27 Basic earnings "
    "per share (in Korean won) W 132,126 W 189,546 W 10,135 W 21,890 Diluted earnings per share (in Korean won) W 131,478 W 188,700 W 10,050 W 21,700"
)
NOTE_2026Q2 = (
    f"{HEAD} Notes to the Condensed Consolidated Interim Financial Statements June 30, 2026 and 2025 27. Earnings per Share (1) Basic earnings per "
    "share (In millions of Korean won, except for shares and per share amounts) Profit attributable to ordinary shareholders W 93,820,236 134,150,412 "
    "6,997,228 15,104,309 Weighted average number of outstanding ordinary shares1 710,083,326 707,746,374 690,395,022 690,019,799 Basic earnings per share"
)


def test_second_quarter_report_takes_the_three_month_columns():
    p = parse_report_texts("SK Hynix 2026 Q2", [*BS_2026Q2, IS_2026Q2, NOTE_2026Q2])
    assert p.period == pd.Period("2026Q2") and p.quarterly and p.currency == "KRW"
    assert p.values["revenue"] == [79_318_746, 22_231_952]  # three months this year, three months last year (not the six months)
    assert p.values["profit"] == [93_820_236, 6_997_228]  # attributable to owners, not the total comprehensive income
    assert p.values["eps"] == [132_126, 10_135]
    assert p.values["equity"] == [262_693_228 - 312_618, 120_666_751 - 150_573]  # total equity minus non-controlling interests
    assert p.values["wa_shares"] == [710.083326, 690.395022]


def test_2013_annual_report_with_net_sales_and_backslash_won_sign():
    bs = f"{HEAD} Consolidated Statements of Financial Position December 31, 2013 and 2012 (in millions of Korean won) Total equity 13,072,000 9,745,000 Non-controlling interest 4,757 4,844 Total liabilities and equity \\ 20,797,000"
    income = (
        f"{HEAD} Consolidated Statements of Comprehensive Income Years Ended December 31, 2013 and 2012 (in millions of Korean won, except per share amounts) "
        "Notes Net sales 6 \\ 14,165,102 \\ 10,162,210 Cost of sales 33 (8,864,587) (8,550,989) Profit (loss) attributable to: Equity holders of the Parent "
        "Company \\ 2,872,470 \\ (158,886) Non-controlling interest 387 91 Earnings (loss) per share Basic earnings (loss) per share \\ 4,045 \\ (233)"
    )
    p = parse_report_texts("SK Hynix 2013 Q4", [bs, income])
    assert not p.quarterly and p.values["revenue"] == [14_165_102, 10_162_210]
    assert p.values["profit"] == [2_872_470, -158_886] and p.values["eps"] == [4_045, -233]
    assert p.values["equity"] == [13_072_000 - 4_757, 9_745_000 - 4_844] and "wa_shares" not in p.values


def test_note_reference_before_the_eps_and_losses_label():
    income = (
        f"{HEAD} Consolidated Statements of Comprehensive Income For the years ended December 31, 2023 and 2022 (In millions of won, except per share "
        "information) Note 2023 2022 Revenue 4,26,33 W 32,765,719 44,621,568 Profit (loss) attributable to: Owners of the Parent Company W (9,112,428) "
        "2,229,560 Earnings (loss) per share 31 Basic earnings (losses) per share (in won) 31 W (13,244) W 3,242 Diluted earnings (loss) per share"
    )
    bs = f"{HEAD} Consolidated Statements of Financial Position December 31, 2023 and 2022 Non-controlling interests 10 20 Total equity 53,504,295 63,266,375"
    p = parse_report_texts("SK Hynix 2023 Q4", [bs, income])
    assert p.values["eps"] == [-13_244, 3_242] and p.values["profit"] == [-9_112_428, 2_229_560]


def test_report_of_another_period_or_unit_is_refused():
    with pytest.raises(ValueError, match="September 30"):
        parse_report_texts("SK Hynix 2026 Q3", [*BS_2026Q2, IS_2026Q2])
    with pytest.raises(ValueError, match="millions of KRW"):
        parse_report_texts("SK Hynix 2026 Q2", [*BS_2026Q2, IS_2026Q2.replace("In millions of Korean won", "In billions of Korean won")])


def test_statement_pages_skip_the_contents_and_the_notes():
    contents = "Contents Page Independent Auditors' Review Report 1 Condensed Consolidated Statements of Financial Position 3 Statements of Comprehensive Income 5"
    subsidiaries = f"{HEAD} Notes to the Consolidated Financial Statements 1. Major subsidiaries' summarized statements of financial position"
    equity = f"{HEAD} Consolidated Interim Statements of Changes in Equity Six-month periods ended June 30, 2026 and 2025"
    texts = ["cover", contents, *BS_2026Q2, IS_2026Q2, equity, subsidiaries, NOTE_2026Q2, "later note"]
    assert statement_pages(texts) == [2, 3, 4, 7]


# --- earnings presentations ---

DECK_2015Q1 = (
    "(Unit: KRW Billion) Q1'15 QoQ Q4'14 Q1'14 YoY Revenue 4,818 -6% 5,148 3,743 +29% Gross Profit (Loss) 2,288 -5% 2,419 1,632 +40% "
    "Net Profit (Loss) 1,295 -20% 1,624 802 +61% EPS (Won) Basic 1,780 2,235 1,130 Diluted 1,780 2,235 1,104 Shares Outstanding (Mil. shares) "
    "Basic 728 727 710 Diluted 728 727 727 Gross Margin 47% - 47% 44% +3%p Results of Operation"
)
CAPITAL_2015Q1 = (
    "Capital Structure (Unit: KRW Billion) Q1 '15 QoQ Q4 '14 Q1 '14 YoY Assets 27,393 +510 26,883 22,066 +5,327 Liabilities 8,267 -580 8,847 8,213 +54 "
    "Shareholders' Equity 19,126 +1,089 18,036 13,852 +5,273 Capital Stock 3,658 - 3,658 3,569 +89"
)
DECK_2025Q1 = (
    "'25 Q1 '24 Q4 '24 Q1 Q/Q Y/Y Revenue 17,639 19,767 12,430 -11% +42% Net Profit 8,108 8,006 1,917 +1% +323% EPS (KRW) Basic 11,756 11,611 2,788 "
    "Diluted 11,411 11,571 2,788 Shares Outstanding (Mil. Shares) Basic 690 689 688 Diluted 711 690 689 Income Statement 13 (Unit : KRW Billion)"
)


def test_presentation_of_2015_with_percentage_columns_between_the_quarters():
    p = parse_deck_texts("SK Hynix 2015 Q1", ["cover", DECK_2015Q1, CAPITAL_2015Q1])
    assert p.period == pd.Period("2015Q1") and p.quarterly
    assert p.values == {"revenue": [4_818_000], "profit": [1_295_000], "eps": [1_780], "wa_shares": [728], "equity": [19_126_000]}
    assert "non-controlling interests" in p.notes["profit"] and "non-controlling interests" in p.notes["equity"]
    assert deck_pages(["cover", DECK_2015Q1, "a chart", CAPITAL_2015Q1]) == [1, 3]


def test_presentation_since_2018_with_the_quarters_first_and_a_negative_number():
    p = parse_deck_texts("SK Hynix 2025 Q1", [DECK_2025Q1])
    assert p.values == {"revenue": [17_639_000], "profit": [8_108_000], "eps": [11_756], "wa_shares": [690]}  # no capital structure page: no equity
    loss = DECK_2025Q1.replace("Net Profit 8,108 8,006", "Net Profit -118 8,006").replace("Basic 11,756 11,611", "Basic -173 11,611")
    p = parse_deck_texts("SK Hynix 2025 Q1", [loss])
    assert p.values["profit"] == [-118_000] and p.values["eps"] == [-173]


def test_presentation_of_another_quarter_or_layout_is_refused():
    with pytest.raises(ValueError, match="not for 2025 Q2"):
        parse_deck_texts("SK Hynix 2025 Q2", [DECK_2025Q1])
    with pytest.raises(ValueError, match="column names"):
        parse_deck_texts("SK Hynix 2025 Q1", [DECK_2025Q1.replace("'25 Q1 '24 Q4 '24 Q1", "FY25 FY24")])


# --- the IR website boards and the publication time ---


def earnings_board(*entries):
    return {"filePath": CDN, "list": [
        {"title": f"SK hynix FY{y} Q{q} Earnings Results", "eventDate": when, "fileName1": f"{q}Q{y % 100}_Earnings Presentation_ENG.pdf",
         "fileUrl1": f"/attach/deck{y}{q}.pdf", "fileName2": "call.mp3", "fileUrl2": "/attach/call.mp3"} for y, q, when in entries]}


def reports_board(*files):
    by_year = {}
    for year, name, path in files:
        by_year.setdefault(year, []).append((name, path))
    return {"filePath": CDN, "list": [
        {"title": f"{year} Audit(Review) Report", **{k: v for i, (n, p) in enumerate(fs, 1) for k, v in ((f"fileName{i}", n), (f"fileUrl{i}", p))}}
        for year, fs in by_year.items()]}


def test_earnings_board_call_dates_and_times():
    calls = parse_calls(earnings_board((2026, 3, "Oct 28, 2026/ 9:00 AM KST"), (2024, 2, "Jul 25, 2024 / 9:00 AM KST"), (2022, 4, "Feb 1, 2023")))
    assert calls[(2026, 3)] == {"date": pd.Timestamp("2026-10-28"), "time": "09:00", "presentation": CDN + "/attach/deck20263.pdf"}
    assert calls[(2024, 2)]["time"] == "09:00" and calls[(2022, 4)] == {"date": pd.Timestamp("2023-02-01"), "time": None, "presentation": CDN + "/attach/deck20224.pdf"}


@pytest.mark.parametrize(
    "name, quarter",
    [
        ("SK hynix Inc._2026_2Q_Consolidated_vf.pdf", 2),
        ("SK hynix Inc._2025_4Q_Consolidated_Final_Clean.pdf", 4),
        ("SK hynix Inc._2025_4Q_Separate_Final_Clean.pdf", None),
        ("SK hynix Inc._FY24 Audit Report_Consolidated .PDF", 4),
        ("SKhynix2020_Consolidate_Audit_Report_en.pdf", 4),
        ("SKhynix2019_Non-Consolidate_Audit_Report_en.pdf", None),
        ("SKhynix2021_1Q_Quarterly_Review_En.pdf", 1),
        ("SK hynix_2019 3Q_Consolidated.PDF", 3),
        ("SKhynix2019_1Q_Quarterly_Review_en.pdf.pdf", 1),
    ],
)
def test_report_file_names(name, quarter):
    assert report_period(name, 2020) == quarter


def test_reports_board_keeps_the_consolidated_reports():
    found = parse_reports(reports_board((2025, "SK hynix Inc._2025_4Q_Consolidated.pdf", "/attach/a.pdf"), (2025, "SK hynix Inc._2025_4Q_Separate.pdf", "/attach/b.pdf"),
                                        (2025, "SK hynix_2025_1Q_Consolidated.pdf", "/attach/c.pdf")))
    assert found == {(2025, 4): CDN + "/attach/a.pdf", (2025, 1): CDN + "/attach/c.pdf"}


def test_publication_time_from_the_website_the_list_or_the_close():
    call = {"date": pd.Timestamp("2026-10-28"), "time": "09:00", "presentation": None}
    assert publication(call, None) == (pd.Timestamp("2026-10-28 09:00"), NOTES["website"])
    dated = {"date": pd.Timestamp("2020-01-31"), "time": None, "presentation": None}
    when, note = publication(dated, None)
    assert when == pd.Timestamp("2020-01-31 15:30") and "next trading day" in note
    assert KR.available_date(when) == pd.Timestamp("2020-02-01")  # at the close: the next day (the next trading day once prices are known)
    when, note = publication(dated, (pd.Timestamp("2020-01-31 08:00"), "FactSet"))
    assert when == pd.Timestamp("2020-01-31 08:00") and note.startswith("the time of the earnings conference call from a FactSet event calendar")
    assert publication(dated, (pd.Timestamp("2020-01-30 16:00"), "FactSet"))[0] == pd.Timestamp("2020-01-31 15:30")  # another day: ignored


def test_call_times_list(tmp_path):
    path = tmp_path / "times.csv"
    path.write_text("fiscal_year,quarter,date,time,time_zone,source\n", encoding="utf-8")
    assert read_call_times(path) == {}
    path.write_text("fiscal_year,quarter,date,time,time_zone,source\n2019,4,2020-01-31,16:00,Asia/Seoul,FactSet\n2019,3,2019-10-23,20:00,America/New_York,FactSet\n", encoding="utf-8")
    assert read_call_times(path) == {(2019, 4): (pd.Timestamp("2020-01-31 16:00"), "FactSet"), (2019, 3): (pd.Timestamp("2019-10-24 09:00"), "FactSet")}
    assert read_call_times(tmp_path / "missing.csv") == {}
    path.write_text("year,quarter,date,time,time_zone,source\n2019,4,2020-01-31,16:00,Asia/Seoul,FactSet\n", encoding="utf-8")
    with pytest.raises(ValueError, match="columns"):
        read_call_times(path)


def test_the_shipped_call_times_list_covers_the_quarters_without_a_time_on_the_website():
    times = read_call_times()
    assert set(times) >= {(y, q) for y in range(2014, 2023) for q in range(1, 5)} | {(2013, 4)}
    assert times[(2014, 4)] == (pd.Timestamp("2015-01-28 15:30"), "FactSet") and times[(2017, 3)] == (pd.Timestamp("2017-10-26 13:00"), "FactSet")
    assert KR.available_date(times[(2014, 4)][0]) == pd.Timestamp("2015-01-29")  # at or after the close: the next day
    assert all(t.hour == 9 for p, (t, _) in times.items() if p not in ((2014, 4), (2017, 3)))


# --- the provider, with a fake website ---

def report_pdf(year: int, quarter: int, eps: str = "W 2,000 W 1,000") -> bytes:
    end = {1: "March 31", 2: "June 30", 3: "September 30", 4: "December 31"}[quarter]
    columns = "W 9,000,000 W 8,000,000" if quarter in (1, 4) else "W 9,000,000 W 17,000,000 W 8,000,000 W 15,000,000"
    eps_columns = eps if quarter in (1, 4) else "W 2,000 W 4,000 W 1,000 W 2,000"
    profit = "W 1,400,000 W 700,000" if quarter in (1, 4) else "W 1,400,000 W 2,800,000 W 700,000 W 1,400,000"
    shares = "700,000,000 700,000,000" if quarter in (1, 4) else "700,000,000 700,000,000 700,000,000 700,000,000"
    return text_pdf([
        [f"{HEAD}", "Condensed Consolidated Interim Financial Statements"],
        [f"{HEAD} Consolidated Interim Statements of Financial Position", f"{end}, {year} and December 31, {year - 1}", "Non-controlling interests 100 90",
         "Total equity 50,000,100 40,000,090"],
        [f"{HEAD} Consolidated Interim Statements of Comprehensive Income", f"Periods ended {end}, {year} and {year - 1}",
         "(In millions of Korean won, except per share information)", f"Revenue 4,21 {columns}", f"Profit attributable to: Owners of the Parent Company {profit}",
         f"Basic earnings per share (in Korean won) {eps_columns}"],
        [f"{HEAD} Consolidated Interim Statements of Changes in Equity"],
        [f"{HEAD} Notes to the Condensed Consolidated Interim Financial Statements", "27. Earnings per Share",
         f"Weighted average number of outstanding ordinary shares1 {shares}"],
        ["Another note"] * 3,
    ])


def deck_pdf(year: int, quarter: int) -> bytes:
    this, prev, last = (pd.Period(year=year, quarter=quarter, freq="Q") - k for k in (0, 1, 4))
    label = lambda p: f"'{p.year % 100:02d} Q{p.quarter}"  # noqa: E731
    return text_pdf([
        ["Earnings Results"],
        [f"(Unit : KRW Billion) {label(this)} {label(prev)} {label(last)} Q/Q Y/Y", "Revenue 9,500 9,000 8,000 +6% +19%", "Net Profit 1,450 1,400 700 +4% +107%",
         "EPS (KRW) Basic 2,070 2,000 1,000 Diluted 2,060 1,990 990", "Shares Outstanding (Mil. Shares) Basic 700 700 700 Diluted 701 701 701"],
        ["Capital Structure (Unit : KRW Billion)", "Shareholders' Equity 51,000 50,000 40,000"],
    ])


class FakeHttp:
    def __init__(self, calls, reports, files, broken=()):
        self.boards = {105: earnings_board(*calls), 101: reports_board(*reports)}
        self.files = files
        self.broken = set(broken)
        self.requests = []

    def get_bytes(self, url, params=None, **kwargs):
        if url == API:
            return json.dumps(self.boards[params["bcode"]]).encode()
        self.requests.append(url)
        if url in self.broken:
            raise ConnectionError(f"cannot download {url}")
        return self.files[url]


CALLS = [(2026, 1, "Apr 23, 2026/ 9:00 AM KST"), (2026, 2, "Jul 29, 2026/ 9:00 AM KST"), (2026, 3, "Oct 28, 2026/ 9:00 AM KST")]
SINCE = datetime(2014, 1, 1)


def test_provider_saves_reports_and_the_latest_presentation_as_provisional(tmp_path):
    reports = [(2026, "SK hynix_2026_1Q_Consolidated_Final.pdf", "/attach/r1.pdf"), (2026, "SK hynix Inc._2026_2Q_Consolidated_vf.pdf", "/attach/r2.pdf")]
    files = {CDN + "/attach/r1.pdf": report_pdf(2026, 1), CDN + "/attach/r2.pdf": report_pdf(2026, 2), CDN + "/attach/deck20263.pdf": deck_pdf(2026, 3)}
    http = FakeHttp(CALLS, reports, files)
    provider = SkHynixIrProvider(http, tmp_path, tmp_path / "no-list.csv")
    found = provider.results_announcements("000660.KS", SINCE)
    assert [(f.title, f.published, f.provisional) for f in found] == [
        ("SK Hynix 2026 Q1 consolidated financial statements", pd.Timestamp("2026-04-23 09:00"), False),
        ("SK Hynix 2026 Q2 consolidated financial statements", pd.Timestamp("2026-07-29 09:00"), False),
        ("SK Hynix 2026 Q3 earnings presentation", pd.Timestamp("2026-10-28 09:00"), True),
    ]
    assert found[0].note.startswith(NOTES["website"]) and "review or audit report" in found[0].note
    folder = tmp_path / "000660.KS"
    assert sorted(p.name for p in folder.iterdir()) == ["2026Q1_report.pdf", "2026Q2_report.pdf", "2026Q3_presentation.pdf", "index.json"]
    assert len(PdfReader(folder / "2026Q2_report.pdf").pages) == 3  # the statements and the EPS note page only
    q2 = parse_announcement(found[1], "skhynix_statements", get_market("KR"))
    assert q2.values["revenue"] == [9_000_000, 8_000_000] and q2.values["wa_shares"] == [700.0, 700.0] and q2.note == found[1].note
    q3 = parse_announcement(found[2], "skhynix_statements", get_market("KR"))
    assert q3.provisional and q3.values["equity"] == [51_000_000] and "non-controlling" in q3.series_notes["profit"]
    # the report is out: it replaces the presentation, which is deleted; the saved reports are not downloaded again
    http.boards[101] = reports_board(*reports, (2026, "SK hynix Inc._2026_3Q_Consolidated.pdf", "/attach/r3.pdf"))
    files[CDN + "/attach/r3.pdf"] = report_pdf(2026, 3)
    http.requests.clear()
    found = provider.results_announcements("000660.KS", SINCE)
    assert [(f.title, f.provisional) for f in found][-1] == ("SK Hynix 2026 Q3 consolidated financial statements", False)
    assert http.requests == [CDN + "/attach/r3.pdf"] and not (folder / "2026Q3_presentation.pdf").exists()
    release = provider.release_info("000660.KS")[CDN + "/attach/r3.pdf"]
    assert release.first_published == pd.Timestamp("2026-10-28 09:00") and release.first_url == CDN + "/attach/deck20263.pdf"


def test_a_failing_document_is_an_error_for_its_quarter_only(tmp_path, capsys):
    reports = [(2026, "SK hynix_2026_1Q_Consolidated.pdf", "/attach/r1.pdf"), (2026, "SK hynix_2026_2Q_Consolidated.pdf", "/attach/r2.pdf")]
    files = {CDN + "/attach/r1.pdf": report_pdf(2026, 1), CDN + "/attach/r2.pdf": report_pdf(2026, 2), CDN + "/attach/deck20263.pdf": deck_pdf(2026, 3)}
    provider = SkHynixIrProvider(FakeHttp(CALLS, reports, files, broken=[CDN + "/attach/r1.pdf"]), tmp_path, tmp_path / "no-list.csv")
    found = provider.results_announcements("000660.KS", SINCE)
    assert [f.title.split(" ")[2:4] for f in found] == [["2026", "Q2"], ["2026", "Q3"]]
    assert "ERROR 000660.KS: 2026Q1 cannot be read" in capsys.readouterr().err
    index = json.loads((tmp_path / "000660.KS" / "index.json").read_text(encoding="utf-8"))
    assert [(e["year"], e["quarter"]) for e in index] == [(2026, 2), (2026, 3)]  # what was fetched is saved
    # a report whose text cannot be parsed is not saved either (nothing would read it later), and is tried again on the next update
    files[CDN + "/attach/r1.pdf"] = report_pdf(2026, 1, eps="W 2,000")
    provider = SkHynixIrProvider(FakeHttp(CALLS, reports, files), tmp_path, tmp_path / "no-list.csv")
    assert len(provider.results_announcements("000660.KS", SINCE)) == 2 and "ERROR 000660.KS: 2026Q1" in capsys.readouterr().err
    files[CDN + "/attach/r1.pdf"] = report_pdf(2026, 1)
    assert len(provider.results_announcements("000660.KS", SINCE)) == 3


def test_saved_quarters_are_used_when_the_website_cannot_be_reached(tmp_path):
    files = {CDN + "/attach/deck20263.pdf": deck_pdf(2026, 3)}
    SkHynixIrProvider(FakeHttp(CALLS[2:], [], files), tmp_path, tmp_path / "no-list.csv").results_announcements("000660.KS", SINCE)

    class Down:
        def get_bytes(self, url, **kwargs):
            raise ConnectionError("offline")

    assert len(SkHynixIrProvider(Down(), tmp_path).results_announcements("000660.KS", SINCE)) == 1
    with pytest.raises(ConnectionError):
        SkHynixIrProvider(Down(), tmp_path / "empty").results_announcements("000660.KS", SINCE)


def test_scanned_report_keeps_the_presentation_for_good(tmp_path):
    calls = [(2019, 1, "Apr 25, 2019")]
    scanned = text_pdf([[], []])
    files = {CDN + "/attach/r.pdf": scanned, CDN + "/attach/deck20191.pdf": deck_pdf(2019, 1)}
    # first only the presentation (provisional), then the report comes out as a scan: the presentation stays, no longer provisional
    http = FakeHttp(calls, [], files)
    provider = SkHynixIrProvider(http, tmp_path, tmp_path / "no-list.csv")
    assert [f.provisional for f in provider.results_announcements("000660.KS", SINCE)] == [True]
    http.boards[101] = reports_board((2019, "SKhynix2019_1Q_Quarterly_Review_en.pdf", "/attach/r.pdf"))
    found = provider.results_announcements("000660.KS", SINCE)
    assert [(f.title, f.provisional, f.published) for f in found] == [("SK Hynix 2019 Q1 earnings presentation", False, pd.Timestamp("2019-04-25 15:30"))]
    assert "scanned image" in found[0].note and "next trading day" in found[0].note
    # straight from a scan in a fresh folder
    fresh = SkHynixIrProvider(FakeHttp(calls, [(2019, "SKhynix2019_1Q_Quarterly_Review_en.pdf", "/attach/r.pdf")], files), tmp_path / "fresh", tmp_path / "no-list.csv")
    assert [(f.title, f.provisional) for f in fresh.results_announcements("000660.KS", SINCE)] == [("SK Hynix 2019 Q1 earnings presentation", False)]


def test_a_call_time_listed_later_changes_the_saved_publication_time(tmp_path):
    calls = [(2019, 1, "Apr 25, 2019")]
    files = {CDN + "/attach/deck20191.pdf": deck_pdf(2019, 1)}
    times = tmp_path / "times.csv"
    provider = SkHynixIrProvider(FakeHttp(calls, [], files), tmp_path, times)
    assert provider.results_announcements("000660.KS", SINCE)[0].published == pd.Timestamp("2019-04-25 15:30")
    times.write_text("fiscal_year,quarter,date,time,time_zone,source\n2019,1,2019-04-25,08:00,Asia/Seoul,FactSet\n", encoding="utf-8")
    found = provider.results_announcements("000660.KS", SINCE)
    assert found[0].published == pd.Timestamp("2019-04-25 08:00") and found[0].note.startswith("the time of the earnings conference call from a FactSet")


def test_store_parses_again_a_filing_whose_publication_time_changed(tmp_path):
    from valuation_monitor.store import EarningsStore

    store = EarningsStore(tmp_path)
    deck = Filing("SK Hynix 2019 Q1 earnings presentation", pd.Timestamp("2019-04-25 15:30"), CDN + "/attach/deck20191.pdf", deck_pdf(2019, 1), provisional=True)
    splits = pd.Series(dtype=float)
    first = store.load("000660.KS", [deck], splits, fmt="skhynix_statements", market=KR)
    assert first.announcements[0].published == pd.Timestamp("2019-04-25 15:30") and first.announcements[0].provisional
    deck.published, deck.provisional = pd.Timestamp("2019-04-25 08:00"), False
    again = store.load("000660.KS", [deck], splits, fmt="skhynix_statements", market=KR)
    assert again.announcements[0].published == pd.Timestamp("2019-04-25 08:00") and not again.announcements[0].provisional
    assert not any("provisional" in d or "presentation (before" in d for d in again.discrepancies())


def test_provisional_note_names_the_presentation(tmp_path):
    from valuation_monitor.store import EarningsStore

    deck = Filing("SK Hynix 2019 Q1 earnings presentation", pd.Timestamp("2019-04-25 15:30"), CDN + "/attach/deck20191.pdf", deck_pdf(2019, 1), provisional=True)
    history = EarningsStore(tmp_path).load("000660.KS", [deck], pd.Series(dtype=float), fmt="skhynix_statements", market=KR)
    assert any(d.startswith("The 2019Q1 data comes from the earnings presentation") for d in history.discrepancies())


def test_config_and_source():
    stock = parse_config({"stocks": [{"symbol": "000660.KS", "name": "SK Hynix", "filing_format": "skhynix_statements", "metrics": ["pe", "ps", "pb"]}]}).stocks[0]
    assert (stock.market, stock.filing_format) == ("KR", "skhynix_statements")
    assert stock.filing_source.startswith("SK Hynix IR website") and PARSER_VERSIONS["skhynix_statements"] == 1
    assert parse_config({"stocks": [{"symbol": "005930.KS", "name": "Samsung Electronics"}]}).stocks[0].filing_source.startswith("Samsung")
    with pytest.raises(ValueError, match="does not match market"):
        parse_config({"stocks": [{"symbol": "2395.TW", "filing_format": "skhynix_statements"}]})


# --- the saved excerpts (data/filings/000660.KS) ---

saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved SK Hynix filings")


@saved
def test_every_saved_filing_parses_and_its_shares_match_profit_over_eps():
    index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    assert index and all((FOLDER / e["file"]).exists() for e in index)
    for e in index:
        filing = Filing(f"SK Hynix {e['year']} Q{e['quarter']} " + ("earnings presentation" if e["kind"] == "presentation" else "consolidated financial statements"),
                        pd.Timestamp(e["published"]), e["url"], (FOLDER / e["file"]).read_bytes(), provisional=bool(e.get("provisional")))
        a: Announcement = parse_announcement(filing, "skhynix_statements", KR)
        assert a.period == pd.Period(year=e["year"], quarter=e["quarter"], freq="Q")
        implied = a.values["profit"][0] / a.values["eps"][0]
        assert abs(a.values["wa_shares"][0] / implied - 1) < 0.01, (e["year"], e["quarter"], implied)  # presentations: rounded, net profit incl. NCI
        assert a.values["equity"][0] > 0


@saved
def test_saved_scanned_quarters_come_from_the_presentations():
    index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    permanent = {(e["year"], e["quarter"]) for e in index if e["kind"] == "presentation" and not e.get("provisional")}
    assert {(2015, 1), (2016, 3), (2016, 4), (2017, 1), (2017, 2), (2018, 1), (2019, 1)} <= permanent
