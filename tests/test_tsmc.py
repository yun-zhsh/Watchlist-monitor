"""TSMC (TSM, NYSE ADR): the tsmc_release layout (earnings release, conference slides, consolidated financial statements excerpts), the SEC
provider's TSMC mode, and the supplementary equity in build_history; on text fixtures and on the filings saved in the repo (data/filings/TSM)."""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor import audit
from valuation_monitor.config import parse_config
from valuation_monitor.filings import adjust_for_splits, build_history, parse_announcement
from valuation_monitor.market_calendar import get_market
from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.sec import SLIDES_NOTE, SecProvider, tsmc_equity_gaps
from valuation_monitor.tsmc_release import (
    ADR_SHARES,
    STATEMENTS_NOTE,
    parse_release,
    parse_slides,
    parse_statements,
    slides_title,
    statements_excerpt,
    statements_title,
    title,
)

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "TSM"
US = get_market("US")


def release_html(quarter="3Q26", prior="3Q25", previous="2Q26", word="Third", eps=("29.43 b", "17.44 c", "27.25 b"), shares=("b: Based on 25,932", "c: Based on 25,930"),
                 split=False, revenue=("1,494,243", "989,918", "1,270,381"), extra="") -> bytes:
    header = f"<tr><td>{quarter} Amount a</td><td>{prior} Amount</td><td>YoY Inc. (Dec.) %</td><td>{previous} Amount</td><td>QoQ Inc. (Dec.) %</td></tr>"
    first = (f"<tr><td>Net sales</td><td>{revenue[0]}</td><td>{revenue[1]}</td><td>50.9</td><td>{revenue[2]}</td><td>17.6</td></tr>"
             "<tr><td>Gross profit</td><td>992,379</td><td>588,543</td><td>68.6</td><td>860,311</td><td>15.4</td></tr>")
    second = ("<tr><td>Net income</td><td>763,064</td><td>452,302</td><td>68.7</td><td>706,562</td><td>8.0</td></tr>"
              f"<tr><td>EPS (NT$)</td><td>{eps[0]}</td><td>{eps[1]}</td><td>68.7</td><td>{eps[2]}</td><td>8.0</td></tr>")
    tables = f"<table>{header}{first}</table><table></table><table>{second}</table>" if split else f"<table>{header}{first}{second}</table>"
    notes = "".join(f"<p>{s} million weighted average outstanding shares</p>" for s in shares)
    head_eps = eps[0].split()[0]
    return (
        f"<html><body><p>EX-99.1</p><p>TSMC Reports {word} Quarter EPS of NT${head_eps}</p><p>HSINCHU, Taiwan -- TSMC today announced consolidated revenue of "
        f"NT$1,494.24 billion, net income of NT$763.06 billion, and diluted earnings per share of NT${head_eps} (US$4.60 per ADR unit).</p>"
        f"<p>(Unit: NT$ million, except for EPS)</p>{tables}<p>a: figures have not been approved by Board of Directors</p>{notes}{extra}</body></html>"
    ).encode()


def slides_html(body: str, deck="2026 Third Quarter Earnings Conference") -> bytes:
    padding = "<p>Unleash Innovation</p>" * 50  # the decks are 4-45 KB; the provider skips documents under 1 KB
    return f"<html><body><p>EX-99.2 {deck} October 15, 2026</p><p>Balance Sheets &amp; Key Indices {body}</p>{padding}</body></html>".encode()


SLIDES_2026 = slides_html(
    "Selected Items from Balance Sheets 3Q26 2Q26 3Q25 (In NT$ billions) Amount % Amount % Amount % Total Liabilities 3,009.66 29.9 % 2,901.18 30.9 % "
    "2,318.53 31.5 % Total Shareholders' Equity 7,043.23 70.1 % 6,474.47 69.1 % 5,035.58 68.5 % Key Indices"
)


# ---- the earnings release ---------------------------------------------------------------------------------------------------


def test_the_release_gives_revenue_net_income_and_diluted_eps_per_adr_and_shares_in_adr():
    parsed = parse_release(release_html())
    assert parsed.currency == "TWD" and parsed.period == pd.Period("2026Q3") and parsed.quarterly
    assert parsed.values["revenue"] == [1494243.0, 989918.0, 1270381.0]
    assert parsed.values["profit"] == [763064.0, 452302.0, 706562.0]
    assert ADR_SHARES == 5
    assert parsed.values["eps"] == [147.15, 87.2, 136.25]  # NT$ per share x 5
    assert parsed.values["wa_shares"] == [5186.4, 5186.0, 5186.4]  # each EPS by its own footnote mark: 25,932 and 25,930 million shares / 5
    assert "equity" not in parsed.values
    assert "diluted EPS" in parsed.notes["eps"] and "x 5" in parsed.notes["eps"]
    assert title(release_html()) == "TSMC Reports Third Quarter EPS of NT$29.43"


def test_older_layouts_marks_in_cells_of_their_own_a_table_in_two_parts_and_stray_spaces():
    old = release_html("1Q15", "1Q14", "4Q14", "First", ("3.05", "1.85", "3.08"), ("b : Based on 25,93 1", "c: Based on 25,930"),
                       extra="")  # 2015: the marks are cells of their own (below); "25,93 1": a number broken by a stray space
    old = old.replace(b"<td>3.05</td>", b"<td>3.05</td><td>b</td>").replace(b"<td>1.85</td>", b"<td>1.85</td><td>c</td>").replace(b"<td>3.08</td>", b"<td>3.08</td><td>c</td>")
    parsed = parse_release(old)
    assert parsed.period == pd.Period("2015Q1") and parsed.values["eps"] == [15.25, 9.25, 15.4]
    assert parsed.values["wa_shares"] == [5186.2, 5186.0, 5186.0]
    assert parse_release(release_html(split=True)).values == parse_release(release_html()).values  # 2024: the table goes on after empty ones
    spaced = parse_release(release_html(eps=("29.43 b", "17. 44 c", "27.25 b")))  # "5. 18 b" (2022)
    assert spaced.values["eps"][1] == 87.2


@pytest.mark.parametrize(
    "html, message",
    [
        (release_html(previous="1Q26"), "unexpected columns"),
        (release_html(word="Second"), "disagree on the quarter"),
        (release_html(shares=("b: Based on 25,932",)), "no weighted average shares"),
        (release_html(revenue=("1,494,243", "n/a", "1,270,381")), "unexpected row"),
        (release_html().replace(b"diluted earnings per share of NT$29.43", b"diluted earnings per share of NT$29.44"), "does not match the table"),
        (release_html().replace(b"<tr><td>Net income</td>", b"<tr><td>Net profit</td>"), "rows missing"),
        (b"<html><body><p>TSMC Board of Directors Meeting Resolutions</p></body></html>", "not a TSMC earnings release"),
    ],
)
def test_a_release_that_does_not_fit_is_an_error(html, message):
    with pytest.raises(ValueError, match=message):
        parse_release(html)


# ---- the slides -------------------------------------------------------------------------------------------------------------


def test_the_slides_give_the_total_shareholders_equity_of_the_quarter():
    assert slides_title(SLIDES_2026) == "2026 Third Quarter Earnings Conference"
    assert parse_slides(SLIDES_2026) == (pd.Period("2026Q3"), 7043230.0)  # NT$ billions to millions
    parsed = parse_release(release_html(), SLIDES_2026)  # the slides of the same 6-K
    assert parsed.values["equity"] == [7043230.0] and "non-controlling interests" in parsed.notes["equity"]


@pytest.mark.parametrize(
    "body, deck, period, equity",
    [
        # the cells run together (2016Q2)
        ("Selected Items from Balance Sheets 2Q16 1Q162Q15 (In NT$ billions) Amount %Amount%Amount% Total Shareholders’ Equity 1,197.33 66.4%1,280.9574.2%1,066.6366.3% Key",
         "2016 Second Quarter Earnings Conference", "2016Q2", 1197330.0),
        # no quarter labels in the text (2017Q3), no spaces
        ("Selected Items from Balance Sheets(In NT$ billions)Amount%Amount%Amount% Total Shareholders’ Equity1,431.41 77.7%1,342.3667.6%1,283.8974.7%Key",
         "2017 Third Quarter Earnings Conference", "2017Q3", 1431410.0),
        # "In$NT billions" (2017Q2) and "b illions" (2023-2025)
        ("Selected Items from Balance Sheets 2Q17 1Q17 2Q16 (In$NT billions) Amount % Total Shareholders’ Equity 1,342.36 67.6% 1,456.31 75.3% 1,197.33 66.4% Key",
         "2017 Second Quarter Earnings Conference", "2017Q2", 1342360.0),
        ("Selected Items from Balance Sheets (In NT$ b illions) Amount % Total Shareholders' Equity 3,092.89 61.3% 2,960.49 59.6% 2,321.47 58.1% Key",
         "2023 First Quarter Earnings Conference", "2023Q1", 3092890.0),
    ],
)
def test_slides_text_variants(body, deck, period, equity):
    assert parse_slides(slides_html(body, deck)) == (pd.Period(period), equity)


def test_slides_whose_labels_do_not_fit_the_deck_or_whose_pages_are_images():
    wrong = slides_html("Selected Items from Balance Sheets 2Q26 1Q26 2Q25 (In NT$ billions) Total Shareholders' Equity 7,043.23 70.1 % 6,474.47 69.1 % 5,035.58 68.5 %")
    with pytest.raises(ValueError, match="do not fit the deck"):
        parse_slides(wrong)
    images = b"<html><body><p>EX-99.2 3 tsm-ex992_6.htm 2020 Third Quarter Earnings Conference</p><img src='a.jpg'></body></html>"
    assert slides_title(images) == "2020 Third Quarter Earnings Conference" and parse_slides(images) is None
    assert "equity" not in parse_release(release_html(), images).values
    with pytest.raises(ValueError, match="the slides are for"):
        parse_release(release_html(), slides_html(SLIDES_2026.decode().split("Balance Sheets &amp; Key Indices ")[1].split("</p>")[0].replace("3Q26 2Q26 3Q25", "2Q26 1Q26 2Q25"),
                                                  "2026 Second Quarter Earnings Conference"))


# ---- the consolidated financial statements ----------------------------------------------------------------------------------


def statements_html(title_text="Consolidated Financial Statements for the Nine Months Ended September 30, 2019 and 2018 and Independent Auditors' Review Report",
                    first_date="September 30, 2019") -> bytes:
    return (
        "<html><body><p>EX-99.1 English Translation of Financial Statements Originally Issued in Chinese</p>"
        f"<p>Taiwan Semiconductor Manufacturing Company Limited and Subsidiaries {title_text}</p><p>INDEPENDENT AUDITORS' REVIEW REPORT ...</p>"
        "<p>Taiwan Semiconductor Manufacturing Company Limited and Subsidiaries CONSOLIDATED BALANCE SHEETS (In Thousands of New Taiwan Dollars)</p>"
        f"<TABLE><tr><td>{first_date}</td><td>December 31, 2018</td><td>September 30, 2018</td></tr>"
        "<tr><td>Equity attributable to shareholders of the parent</td><td>1,586,812,330</td><td>1,676,817,665</td></tr>"
        "<tr><td>NON - CONTROLLING INTERESTS</td><td>684,671</td><td>678,731</td></tr>"
        "<tr><td>Total equity</td><td>$ 1,587,497,001</td><td>1,677,496,396</td></tr></TABLE>"
        "<p>CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME</p>" + "<p>notes</p>" * 1000 + "</body></html>"
    ).encode()


def test_the_statements_give_the_total_equity_and_only_an_excerpt_is_kept():
    html = statements_html()
    assert statements_title(html).endswith("for the Nine Months Ended September 30, 2019")
    excerpt = statements_excerpt(html)
    assert excerpt.startswith(html[:200]) and excerpt.endswith(b"</TABLE>\n</body></html>\n") and len(excerpt) < len(html) / 2
    assert parse_statements(excerpt) == parse_statements(html) == (pd.Period("2019Q3"), 1587497.001)  # total equity, NT$ thousands to millions
    annual = statements_html("Consolidated Financial Statements for the Years Ended December 31, 2015 and 2014 and Independent Auditors' Report", "December 31, 2015")
    assert parse_statements(annual)[0] == pd.Period("2015Q4")
    with pytest.raises(ValueError, match="is not the end of"):
        parse_statements(statements_html(first_date="December 31, 2018"))
    parent_only = statements_html().replace(b"and Subsidiaries Consolidated Financial", b"Parent Company Only Financial")
    assert statements_title(parent_only) is None


# ---- parse results and the history ------------------------------------------------------------------------------------------


def filing(html, published, url, **kwargs) -> Filing:
    return Filing("t", pd.Timestamp(published), url, html, **kwargs)


def test_the_announcement_is_diluted_and_the_formulas_say_so():
    a = parse_announcement(filing(release_html(), "2026-10-15 06:40", "r", attachments={"slides": SLIDES_2026}), "tsmc_release", US)
    assert a.shares_label == audit.DILUTED_SHARES_LABEL and a.values["equity"] == [7043230.0]
    rec = build_history([a]).ttm_detail[-1]
    assert audit.eps_kind(rec) == "diluted"
    assert audit.explain("eps_ttm", rec)[0].startswith("TTM EPS (sum of the diluted EPS of each period)")
    assert "/ diluted EPS 147.15 implies" in audit.shares_formula(rec)
    assert audit.eps_kind({"shares_label": "basic EPS weighted average shares"}) == "basic"  # every other stock as before


def quarters_of_releases():
    """Four quarters of releases (2025Q4-2026Q3): enough for a TTM."""
    rows = [("4Q25", "4Q24", "3Q25", "Fourth", "2026-01-15 06:58"), ("1Q26", "1Q25", "4Q25", "First", "2026-04-16 08:00"),
            ("2Q26", "2Q25", "1Q26", "Second", "2026-07-16 07:45"), ("3Q26", "3Q25", "2Q26", "Third", "2026-10-15 06:40")]
    return [parse_announcement(filing(release_html(q, p, v, w), t, f"r{q}"), "tsmc_release", US) for q, p, v, w, t in rows]


def test_equity_from_a_later_document_is_used_from_its_own_publication_only():
    releases = quarters_of_releases()
    slides = parse_announcement(filing(SLIDES_2026, "2026-10-16 06:10", "s3Q26", note=SLIDES_NOTE, supplement=True), "tsmc_release", US)
    assert slides.supplement and slides.values == {"equity": [7043230.0]} and slides.period == pd.Period("2026Q3")
    history = build_history(adjust_for_splits([*releases, slides], pd.Series(dtype=float)))
    ttm = history.ttm
    assert ttm["equity"].isna().tolist() == [True, True, True, True, False]  # the release has no equity; the slides add a row of their own
    assert ttm["available_date"].iloc[-1] == pd.Timestamp("2026-10-16") and ttm["eps_ttm"].iloc[-1] == ttm["eps_ttm"].iloc[-2]
    assert history.ttm_detail[-1]["note"] == "equity from the earnings conference slides published 2026-10-16 06:10"
    assert ttm["book_value_per_share"].iloc[-1] == round(7043230.0 / 5186.4, 4)

    same_day = parse_announcement(filing(SLIDES_2026, "2026-10-15 07:00", "s3Q26", note=SLIDES_NOTE, supplement=True), "tsmc_release", US)
    ttm = build_history([*releases, same_day]).ttm
    assert len(ttm) == 4 and ttm["equity"].iloc[-1] == 7043230.0  # available the same day: that row is completed

    statements = parse_announcement(filing(statements_html(), "2019-11-19 06:27", "fs", note=STATEMENTS_NOTE, supplement=True), "tsmc_release", US)
    assert statements.values == {"equity": [1587497.001]} and "consolidated financial statements" in statements.series_notes["equity"]

    older = parse_announcement(filing(slides_html(SLIDES_2026.decode().split("Indices ")[1].split("</p>")[0].replace("3Q26 2Q26 3Q25", "2Q26 1Q26 2Q25"),
                                                 "2026 Second Quarter Earnings Conference"), "2026-10-20 06:00", "s2Q26", note=SLIDES_NOTE, supplement=True), "tsmc_release", US)
    assert len(build_history([*releases, older]).ttm) == 4  # the equity of an earlier quarter adds no row to the latest one


def test_tsmc_release_needs_its_own_exhibit_mode():
    base = {"symbol": "TSM", "market": "US", "cik": 1046179, "metrics": ["pe", "ps", "pb"], "eps_basis": {"pe": "diluted"}}
    config = parse_config({"stocks": [{**base, "filing_format": "tsmc_release", "sec_exhibit": "tsmc_release"}]})
    assert config.stocks[0].metric_label(__import__("valuation_monitor.metrics", fromlist=["METRICS"]).METRICS["pe"]) == "Diluted PE (TTM)"
    with pytest.raises(ValueError, match="tsmc_release together"):
        parse_config({"stocks": [{**base, "filing_format": "tsmc_release"}]})
    with pytest.raises(ValueError, match="does not match market"):
        parse_config({"stocks": [{**base, "symbol": "2330.TW", "market": "TW", "filing_format": "tsmc_release"}]})


# ---- the SEC provider -------------------------------------------------------------------------------------------------------

SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["6-K"] * 5,
            "accessionNumber": [f"0001046179-26-00000{k}" for k in range(1, 6)],
            "acceptanceDateTime": [
                "2026-07-16T11:45:43.000Z",  # 1: 2Q26 release + image-only slides
                "2026-08-10T10:28:44.000Z",  # 2: a monthly revenue report
                "2026-08-14T10:02:33.000Z",  # 3: the 2Q26 consolidated financial statements (fails on the first run)
                "2026-10-15T10:40:56.000Z",  # 4: 3Q26 release + slides with a text balance sheet
                "2026-10-16T10:08:01.000Z",  # 5: slides alone (as until 2019)
            ],
        },
        "files": [],
    }
}
IMAGE_SLIDES = b"<html><body><p>EX-99.2 2026 Second Quarter Earnings Conference</p>" + b"<img src='p1.jpg'>" * 100 + b"</body></html>"


def listing(*files):
    return json.dumps({"directory": {"item": [{"name": n, "size": str(s)} for n, s in files]}}).encode()


class FakeTsmcHttp:
    def __init__(self, failing=()):
        self.requests, self.failing = [], set(failing)

    def get_json(self, url, params=None):
        return SUBMISSIONS

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        folder, name = url.split("/")[-2:]
        files = {
            "000104617926000001": {"cover.htm": b"<html>6-K</html>", "rel2.htm": release_html("2Q26", "2Q25", "1Q26", "Second", ("27.25 b", "15.36 b", "22.08 b")), "pres2.htm": IMAGE_SLIDES},
            "000104617926000002": {"revenue.htm": b"<html><p>TSMC July 2026 Revenue Report</p></html>"},
            "000104617926000003": {"fs.htm": statements_html("Consolidated Financial Statements for the Six Months Ended June 30, 2026 and 2025 and Independent Auditors' Review Report", "June 30, 2026")},
            "000104617926000004": {"rel3.htm": release_html(), "pres3.htm": SLIDES_2026},
            "000104617926000005": {"pres3b.htm": SLIDES_2026},
        }[folder]
        if name == "index.json":  # the statements are large (2-12 MB): the release mode does not download them
            return listing(*((n, 5_000_000 if n == "fs.htm" else len(c)) for n, c in files.items()))
        if folder in self.failing:
            raise ConnectionError("connection reset")
        return files[name]


def test_the_provider_keeps_releases_slides_and_statements_excerpts(tmp_path, capsys):
    http = FakeTsmcHttp(failing={"000104617926000003"})
    found = SecProvider(http, tmp_path).results_announcements("TSM", 1046179, datetime(2026, 1, 1), exhibit="tsmc_release")
    index = json.loads((tmp_path / "TSM" / "index.json").read_text(encoding="utf-8"))
    assert [(e["accession"][-1], e["results"], bool(e.get("slides_file"))) for e in index] == [("1", True, False), ("2", False, False), ("3", False, False), ("4", True, True), ("5", False, True)]
    assert tsmc_equity_gaps(index, tmp_path / "TSM") == {pd.Period("2026Q2")}  # image-only slides
    assert "ERROR reading the 6-K 0001046179-26-000003" in capsys.readouterr().err  # only this 6-K; not recorded, so tried again next time
    assert [(f.title, f.supplement, sorted(f.attachments)) for f in found] == [
        ("TSMC Reports Second Quarter EPS of NT$27.25", False, []),
        ("TSMC Reports Third Quarter EPS of NT$29.43", False, ["slides"]),
        ("2026 Third Quarter Earnings Conference", True, []),
    ]
    checked = json.loads((tmp_path / "TSM" / "statements.json").read_text(encoding="utf-8"))
    assert [(e["accession"][-1], e["results"]) for e in checked] == [("1", False), ("2", False)]  # the 6-Ks of the window checked so far; the failed one is not recorded

    found = SecProvider(FakeTsmcHttp(), tmp_path).results_announcements("TSM", 1046179, datetime(2026, 1, 1), exhibit="tsmc_release")
    statements = [f for f in found if f.note == STATEMENTS_NOTE]
    assert len(statements) == 1 and statements[0].supplement and statements[0].published == pd.Timestamp("2026-08-14 06:02:33")
    saved = json.loads((tmp_path / "TSM" / "statements.json").read_text(encoding="utf-8"))
    assert [(e["accession"][-1], e["results"]) for e in saved] == [("1", False), ("2", False), ("3", True)]
    assert statements[0].content == statements_excerpt(statements_html("Consolidated Financial Statements for the Six Months Ended June 30, 2026 and 2025 and Independent Auditors' Review Report", "June 30, 2026"))

    n = len(http.requests)
    offline = SecProvider(None, tmp_path).results_announcements("TSM", 1046179, datetime(2026, 1, 1), exhibit="tsmc_release")
    assert [f.title for f in offline] == [f.title for f in found] and len(http.requests) == n
    parsed = [parse_announcement(f, "tsmc_release", US) for f in offline]
    history = build_history(parsed)
    assert history.quarters["equity"][pd.Period("2026Q2")].value == 1587497.001
    assert history.quarters["equity"][pd.Period("2026Q3")].value == 7043230.0


# ---- the saved filings ------------------------------------------------------------------------------------------------------

saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved TSMC filings")


def saved_filings() -> list[Filing]:
    return SecProvider(None, FOLDER.parent).results_announcements("TSM", 1046179, datetime(2014, 1, 1), exhibit="tsmc_release")


@saved
def test_every_saved_filing_parses_and_net_income_over_eps_agrees_with_the_shares():
    announcements = [parse_announcement(f, "tsmc_release", US) for f in saved_filings()]
    releases = [a for a in announcements if not a.supplement]
    assert len(releases) >= 48
    for a in releases:
        for profit, eps, shares in zip(a.values["profit"], a.values["eps"], a.values["wa_shares"]):
            assert abs(profit / eps / shares - 1) < 0.005, a.url  # EPS rounded to the NT$ cent
    quarters = {a.period for a in releases}
    with_equity = {a.period for a in announcements if "equity" in a.values}
    assert quarters <= with_equity  # every quarter gets its equity: from the slides, or from the consolidated financial statements


@saved
def test_saved_history_has_no_discrepancies_and_the_prior_year_columns_repeat_earlier_figures():
    history = build_history([parse_announcement(f, "tsmc_release", US) for f in saved_filings()])
    assert history.discrepancies() == []
    for series in ("revenue", "profit", "eps"):
        assert all(len(q.observations) >= 2 for p, q in history.quarters[series].items() if pd.Period("2015Q1") <= p <= pd.Period("2025Q3"))
