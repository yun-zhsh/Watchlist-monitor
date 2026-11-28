"""Infosys (INFY, NYSE ADS): the infosys_ifrs layout (IFRS USD press release, IFRS statements in USD as a supplementary document), the SEC
provider's Infosys mode, and the supplement that repeats its quarter's figures in build_history; on text fixtures and on the filings saved
in the repo (data/filings/INFY)."""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.config import parse_config
from valuation_monitor.filings import adjust_for_splits, build_history, parse_announcement
from valuation_monitor.infosys import (
    IMPLIED_SHARES_NOTE,
    STATEMENTS_NOTE,
    parse_release,
    parse_statements,
    release_title,
    statements_excerpt,
    statements_title,
)
from valuation_monitor.market_calendar import get_market
from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.sec import SecProvider

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "INFY"
US = get_market("US")
Q = lambda text: pd.Period(text, freq="Q")  # noqa: E731


def row(*cells: str) -> str:
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def balance(end="September 30, 2025", owners="11,634", nci="53", total="11,687", note=False, year_end="March 31, 2025") -> str:
    head = ("Condensed Consolidated Balance Sheet as at", "Note") if note else ()
    return (
        "<table>" + row(*head, end, year_end) + row("ASSETS") + row("Total assets", "18,064", "17,419")
        + row("Total equity attributable to equity holders of the company", owners, "11,205") + row("Non-controlling interests", nci, "50")
        + row("Total equity", total, "11,255") + "</table>"
    )


def release_html(end="September 30, 2025", prior="September 30, 2024", group="6 months", revenue=("5,076", "4,894"), profit=("839", "777"),
                 eps=("0.20", "0.19"), owners="11,634", start="IFRS USD Press Release", particulars=False, shares=None, extra="", year_end="March 31, 2025") -> bytes:
    lead = ("Particulars",) if particulars else ()
    head = row(*lead, f"3 months ended {end}", f"3 months ended {prior}", f"{group} ended {end}", f"{group} ended {prior}")
    body = (
        row("Revenues", *revenue, "10,018", "9,608") + row("Other income, net (3)", "100", "72", "210", "160")
        + row("Net profit (after non-controlling interest)", *profit, "1,647", "1,540") + row("Basic EPS ($)", *eps, "0.40", "0.37")
        + row("Diluted EPS ($)", *eps, "0.40", "0.37")
    )
    if shares:
        body += row("Weighted average equity shares used in computing earnings per equity share") + row("Basic", *shares, "4,144,593,296", "4,141,043,772")
    return (
        f"<html><body><p>EX-99.2 BYLAWS 3 exv99w02.htm IFRS USD PRESS RELEASE Exhibit 99.2</p><p>{start}</p>"
        "<p>Quarterly revenue crosses $5 bn mark</p>" + "<p>Bengaluru, India: Infosys delivered its results.</p>" * 400 + extra
        + balance(end, owners, year_end=year_end) + f"<table>{head}{body}</table></body></html>"
    ).encode()


def statements_html(end="September 30, 2025", prior="September 30, 2024", shares=("4,145,208,267", "4,141,806,535"), years=False,
                    shares_label="Basic (in shares)", eps_label="Basic ($)", dollars="(Dollars in millions except equity share and per equity share data)",
                    notes_after="<p>Notes to the interim condensed consolidated financial statements</p>" * 50) -> bytes:
    if years:  # until 2016: "Three months ended December 31," over the years
        month_day = end.rsplit(",", 1)[0]
        head = row("Note", f"Three months ended {month_day},", f"Six months ended {month_day},") + row(end[-4:], prior[-4:], end[-4:], prior[-4:])
    else:
        head = row("Condensed Consolidated Statement of Comprehensive Income for the", "Note", "Three months ended", "Six months ended") + row(end, prior, end, prior)
    body = (
        row("Revenues", "2.16", "5,076", "4,894", "10,018", "9,608") + row("Cost of sales", "3,516", "3,400", "6,933", "6,659")
        + row("Profit attributable to:") + row("Owners of the Company", "839", "777", "1,647", "1,540") + row("Non-controlling interests", "1", "1", "2", "2")
        + row("840", "778", "1,649", "1,542") + row("Total comprehensive income attributable to:") + row("Owners of the Company", "538", "809", "1,437", "1,570")
        + row("Earnings per equity share") + row(eps_label, "0.20", "0.19", "0.40", "0.37") + row("Diluted ($)", "0.20", "0.19", "0.40", "0.37")
        + row("Weighted average equity shares used in computing earnings per equity share", "2.13")
        + row(shares_label, "2.13", *shares, "4,144,593,296", "4,141,043,772")
    )
    return (
        "<html><body><p>EX-99.7 7 exv99w07.htm AUDITED INTERIM CONDENSED FINANCIAL STATEMENTS IN COMPLIANCE WITH IFRS IN USD</p>"
        "<p>INDEPENDENT AUDITOR'S REPORT</p>" + "<p>Report on the Audit of the Interim Condensed Consolidated Financial Statements</p>" * 20
        + f"<p>{dollars}</p>" + balance(end, note=True) + f"<p>{dollars}</p><p>Condensed Consolidated Statement of Comprehensive Income</p>"
        + f"<table>{head}{body}</table>" + notes_after + "</body></html>"
    ).encode()


# ---- the press release ---------------------------------------------------------------------------------------------------------

def test_the_press_release_gives_revenue_net_profit_basic_eps_and_equity_without_shares():
    html = release_html()
    assert release_title(html) == "IFRS USD press release for the quarter ended September 30, 2025"
    p = parse_release(html)
    assert p.period == Q("2025Q3") and p.currency == "USD" and p.quarterly
    assert p.values == {"revenue": [5076.0, 4894.0], "profit": [839.0, 777.0], "eps": [0.2, 0.19], "equity": [11634.0]}
    assert p.notes is None


def test_older_and_newer_press_release_variants():
    # until 2020 the press release has the full income statement, with the weighted average shares; the label "Basic" only counts under its heading
    old = parse_release(release_html(shares=("4,240,181,854", "4,347,129,592")))
    assert old.values["wa_shares"] == [4240.181854, 4347.129592]
    # a "Particulars" column (since 2026), "IFRS – USD Press Release" (July 2026), and the fourth quarter with its year columns
    q4 = release_html("March 31, 2026", "March 31, 2025", group="Year", start="IFRS – USD Press Release", particulars=True)
    assert release_title(q4) == "IFRS USD press release for the quarter ended March 31, 2026" and parse_release(q4).period == Q("2026Q1")
    # the reconciliation of the adjusted (non-IFRS) figures has a "Basic EPS ($)" row too, but no "3 months ended" header: never read
    adjusted = "<table>" + row("December 31, 2025", "December 31, 2024") + row("Net profit (after non-controlling interest)", "747", "108", "855", "804") + row("Basic EPS ($)", "0.18", "0.03", "0.21", "0.19") + "</table>"
    p = parse_release(release_html("December 31, 2025", "December 31, 2024", group="9 months", profit=("747", "804"), eps=("0.18", "0.19"), extra=adjusted))
    assert p.values["eps"] == [0.18, 0.19] and p.values["profit"] == [747.0, 804.0]


def test_the_equity_typo_of_march_2018_is_read_only_when_total_equity_confirms_it():
    p = parse_release(release_html("March 31, 2018", "March 31, 2017", group="Year", owners="9.960").replace(b"11,687", b"9,960").replace(b"<td>53</td>", b"<td>0</td>"))
    assert p.values["equity"] == [9960.0] and "writes 9.960 for 9,960" in p.notes["equity"]
    with pytest.raises(ValueError, match="is not total equity less non-controlling interests"):
        parse_release(release_html(owners="11,600"))


@pytest.mark.parametrize(
    "html",
    [
        release_html(start="IFRS INR Press Release"),  # the rupee press release
        release_html(start="Press Release - Infosys Board Appoints a CEO designate"),
        release_html(prior="June 30, 2025"),  # the second column is not the same quarter last year
    ],
)
def test_other_documents_are_not_a_press_release(html):
    assert release_title(html) is None


# ---- the statements ------------------------------------------------------------------------------------------------------------

def test_the_statements_give_the_weighted_average_shares_and_only_an_excerpt_is_kept():
    html = statements_html()
    assert statements_title(html) == "IFRS USD condensed consolidated financial statements for the quarter ended September 30, 2025"
    excerpt = statements_excerpt(html)
    assert len(excerpt) < len(html) and b"Notes to the interim" not in excerpt and excerpt.endswith(b"</body></html>\n")
    p = parse_statements(excerpt)
    assert p.period == Q("2025Q3")
    assert p.values == {"revenue": [5076.0, 4894.0], "profit": [839.0, 777.0], "eps": [0.2, 0.19], "wa_shares": [4145.208267, 4141.806535], "equity": [11634.0]}


def test_statements_variants():
    years = statements_html("December 31, 2014", "December 31, 2013", shares=("1,142,805,132", "1,142,805,132"), shares_label="Basic")
    assert parse_statements(statements_excerpt(years)).values["wa_shares"] == [1142.805132, 1142.805132]
    old = statements_html("December 31, 2014", "December 31, 2013", years=True, shares_label="Basic")
    assert parse_statements(statements_excerpt(old)).period == Q("2014Q4")
    assert parse_statements(statements_excerpt(statements_html(eps_label="Basic (in $ per share)"))).values["eps"] == [0.2, 0.19]  # 2023


def test_other_statements_are_not_taken():
    assert statements_title(statements_html(dollars="(In ₹ crore, except equity share and per equity share data)")) is None  # the rupee statements
    assert statements_title(release_html()) is None  # the press release
    annual = statements_html().replace(b"Three months ended", b"Year ended")  # the audited annual statements until 2020: no three months
    assert statements_title(annual) is None


# ---- the history: shares implied until the statements arrive --------------------------------------------------------------------

def filing(html, published, url, **kwargs) -> Filing:
    return Filing("t", pd.Timestamp(published), url, html, **kwargs)


def four_releases():
    rows = [("December 31, 2024", "December 31, 2023", "9 months", "2025-01-16 12:35"), ("March 31, 2025", "March 31, 2024", "Year", "2025-04-17 13:03"),
            ("June 30, 2025", "June 30, 2024", "3 months", "2025-07-23 10:47"), ("September 30, 2025", "September 30, 2024", "6 months", "2025-10-16 11:20")]
    out = []
    for end, prior, group, published in rows:
        html = release_html(end, prior, group=group, year_end="March 31, 2024" if end == "March 31, 2025" else "March 31, 2025")
        out.append(parse_announcement(filing(html, published, f"r{end}"), "infosys_ifrs", US))
    return out


def test_the_press_release_implies_the_shares_and_the_statements_bring_them_from_their_own_publication():
    releases = four_releases()
    assert releases[-1].note == IMPLIED_SHARES_NOTE and "wa_shares" not in releases[-1].values
    statements = parse_announcement(filing(statements_html(), "2025-10-21 09:53", "s", note=STATEMENTS_NOTE, supplement=True), "infosys_ifrs", US)
    assert statements.supplement and statements.period == Q("2025Q3") and statements.values["wa_shares"] == [4145.208267]
    history = build_history(adjust_for_splits([*releases, statements], pd.Series(dtype=float)))
    ttm, detail = history.ttm, history.ttm_detail
    assert len(ttm) == 5 and ttm["available_date"].iloc[-1] == pd.Timestamp("2025-10-21")
    assert ttm["shares"].iloc[-2] == round(839 / 0.2, 3) and detail[-2]["shares_source"] == "implied" and detail[-2]["note"] == IMPLIED_SHARES_NOTE
    assert ttm["shares"].iloc[-1] == 4145.208 and detail[-1]["shares_source"] == "reported"
    assert detail[-1]["note"] == "weighted average shares from the IFRS statements published 2025-10-21 09:53"  # the implied note is gone
    assert ttm["eps_ttm"].iloc[-1] == ttm["eps_ttm"].iloc[-2] and ttm["book_value_per_share"].iloc[-1] == round(11634 / 4145.208267, 4)
    assert history.discrepancies() == []

    # a release that already discloses the shares (until 2020): the same figures again add nothing
    with_shares = [*releases[:-1], parse_announcement(filing(release_html(shares=("4,145,208,267", "4,141,806,535")), "2025-10-16 11:20", "r"), "infosys_ifrs", US)]
    assert len(build_history([*with_shares, statements]).ttm) == 4


def test_a_figure_that_differs_in_the_statements_replaces_the_first_published_one_from_their_publication():
    releases = four_releases()
    statements = parse_announcement(filing(statements_html().replace(b"<td>5,076</td>", b"<td>5,080</td>"), "2025-10-21 09:53", "s", note=STATEMENTS_NOTE, supplement=True), "infosys_ifrs", US)
    history = build_history([*releases, statements])
    q = history.quarters["revenue"][Q("2025Q3")]
    assert q.value == 5080.0 and q.note == "from the IFRS statements published 2025-10-21 09:53; first published as 5,076"
    assert history.ttm["revenue_ttm"].iloc[-1] - history.ttm["revenue_ttm"].iloc[-2] == 4.0
    assert history.ttm_detail[-1]["note"] == "revenue, weighted average shares from the IFRS statements published 2025-10-21 09:53"


def test_statements_of_a_quarter_without_a_release_change_nothing():
    releases = four_releases()
    other = parse_announcement(filing(statements_html("June 30, 2026", "June 30, 2025"), "2026-07-28 08:52", "s", note=STATEMENTS_NOTE, supplement=True), "infosys_ifrs", US)
    assert build_history([*releases, other]).ttm.equals(build_history(releases).ttm)


def test_infosys_ifrs_needs_its_own_exhibit_mode():
    base = {"symbol": "INFY", "market": "US", "cik": 1067491, "metrics": ["pe", "ps", "pb"], "fiscal_year_end": 3}
    config = parse_config({"stocks": [{**base, "filing_format": "infosys_ifrs", "sec_exhibit": "infosys_ifrs"}]})
    assert config.stocks[0].sec_exhibit == "infosys_ifrs"
    with pytest.raises(ValueError, match="infosys_ifrs together"):
        parse_config({"stocks": [{**base, "filing_format": "infosys_ifrs"}]})
    with pytest.raises(ValueError, match="infosys_ifrs together"):
        parse_config({"stocks": [{**base, "filing_format": "us_press_release", "sec_exhibit": "infosys_ifrs"}]})


# ---- the SEC provider ----------------------------------------------------------------------------------------------------------

SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["6-K"] * 3,
            "accessionNumber": [f"0001067491-25-00003{k}" for k in (4, 5, 6)],
            "acceptanceDateTime": ["2025-10-16T15:20:26.000Z", "2025-10-17T13:00:00.000Z", "2025-10-21T13:53:27.000Z"],
        },
        "files": [],
    }
}
NOTICE = b"<html><body><p>Outcome of the Board meeting</p>" + b"<p>text</p>" * 3000 + b"</body></html>"
INR_RELEASE = release_html(start="IFRS INR Press Release")
INR_STATEMENTS = statements_html(dollars="(In ₹ crore, except equity share and per equity share data)", notes_after="<p>notes</p>" * 40000)
USD_STATEMENTS = statements_html(notes_after="<p>Notes to the interim condensed consolidated financial statements</p>" * 8000)


class FakeEdgar:
    def __init__(self):
        self.requests = []

    def get_json(self, url, params=None):
        return SUBMISSIONS

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        folder, name = url.split("/")[-2:]
        files = {
            "000106749125000034": {"index.htm": b"<html>6-K</html>", "exv99w01.htm": NOTICE, "exv99w02.htm": release_html(), "exv99w03.htm": NOTICE},
            "000106749125000035": {"exv99w01.htm": NOTICE},
            "000106749125000036": {"exv99w01.htm": release_html(), "exv99w02.htm": INR_RELEASE, "exv99w07.htm": USD_STATEMENTS, "exv99w08.htm": INR_STATEMENTS},
        }[folder]
        if name == "index.json":
            return json.dumps({"directory": {"item": [{"name": n, "size": str(len(c))} for n, c in files.items()]}}).encode()
        return files[name]


def test_the_provider_keeps_each_press_release_once_and_an_excerpt_of_the_usd_statements(tmp_path, capsys):
    assert len(USD_STATEMENTS) > 400_000 and len(INR_STATEMENTS) > 400_000  # looked at as statements, not as press releases
    http = FakeEdgar()
    found = SecProvider(http, tmp_path).results_announcements("INFY", 1067491, datetime(2025, 1, 1), exhibit="infosys_ifrs")
    assert [(f.title, f.supplement, f.note, str(f.published)) for f in found] == [
        ("IFRS USD press release for the quarter ended September 30, 2025", False, "", "2025-10-16 11:20:26"),
        ("IFRS USD condensed consolidated financial statements for the quarter ended September 30, 2025", True, STATEMENTS_NOTE, "2025-10-21 09:53:27"),
    ]
    index = json.loads((tmp_path / "INFY" / "index.json").read_text(encoding="utf-8"))
    assert [(e["accession"][-1], e["results"], bool(e.get("statements_file"))) for e in index] == [("4", True, False), ("5", False, False), ("6", False, True)]
    assert sorted(p.name for p in (tmp_path / "INFY").iterdir()) == ["2025-10-16_exv99w02.htm", "2025-10-21_exv99w07.htm", "index.json"]  # the release is not saved twice
    assert found[1].content == statements_excerpt(USD_STATEMENTS)
    assert not any(u.endswith("000106749125000036/exv99w08.htm") for u in http.requests)  # the rupee statements come after the dollar ones: not downloaded
    assert "new financial statements 2025-10-21T09:53:27" in capsys.readouterr().err

    n = len(http.requests)
    offline = SecProvider(None, tmp_path).results_announcements("INFY", 1067491, datetime(2025, 1, 1), exhibit="infosys_ifrs")
    assert [f.title for f in offline] == [f.title for f in found] and len(http.requests) == n


def test_the_number_only_exhibit_names_are_tried_only_in_the_infosys_mode(tmp_path):
    found = SecProvider(FakeEdgar(), tmp_path).results_announcements("INFY", 1067491, datetime(2025, 1, 1))  # the usual press release mode
    assert found == []
    index = json.loads((tmp_path / "INFY" / "index.json").read_text(encoding="utf-8"))
    assert [e["results"] for e in index] == [False, False, False]


# ---- the saved filings ---------------------------------------------------------------------------------------------------------

saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved Infosys filings")
SPLITS = pd.Series([2.0], index=[pd.Timestamp("2018-09-12")])  # what Yahoo records within the price history (from November 2015)


def saved_filings() -> list[Filing]:
    return SecProvider(None, FOLDER.parent).results_announcements("INFY", 1067491, datetime(2014, 1, 1), exhibit="infosys_ifrs")


@saved
def test_every_saved_filing_parses_and_net_profit_over_eps_is_near_the_shares():
    announcements = [parse_announcement(f, "infosys_ifrs", US) for f in saved_filings()]
    releases = [a for a in announcements if not a.supplement]
    statements = [a for a in announcements if a.supplement]
    assert len(releases) >= 48 and len(statements) >= 41
    assert len({a.period for a in releases}) == len(releases)  # one press release a quarter
    by_period = {a.period: a for a in releases}
    for s in statements:
        r = by_period[s.period]
        assert all(s.values[k][0] == r.values[k][0] for k in ("revenue", "profit", "eps", "equity")), s.url  # the statements repeat the release
        if "wa_shares" in r.values:
            assert r.values["wa_shares"][0] == s.values["wa_shares"][0]
    for a in announcements:
        if "wa_shares" in a.values:
            assert abs(a.values["profit"][0] / a.values["eps"][0] / a.values["wa_shares"][0] - 1) < 0.04, a.url  # EPS has two decimals


@saved
def test_saved_history_every_quarter_after_the_2015_bonus_issue_gets_disclosed_shares():
    history = build_history(adjust_for_splits([parse_announcement(f, "infosys_ifrs", US) for f in saved_filings()], SPLITS), SPLITS, fiscal_year_end=3)
    shares = history.quarters["wa_shares"]
    without = sorted(str(p) for p in history.quarters["eps"] if p >= Q("2015Q2") and p not in shares)
    assert without == []  # the 2019Q1 shares arrive a year later, with the prior-year column of the 2020Q1 press release
    # the only discrepancies: the June 2015 bonus issue, before Yahoo's split records, and the EPS restated to the cent for the 2018 bonus issue
    for d in history.discrepancies():
        assert any(x in d for x in ("FY2015Q3", "FY2015Q4", "FY2018Q2", "FY2019Q1")), d
