import json
from datetime import datetime

import pandas as pd
import pytest

from valuation_monitor.providers.sec import SecProvider, SecUserAgentMissing, headline, make_http_client, to_new_york

RESULTS_HTML = (
    b"<html><body><p>EX-99.1 Exhibit 99.1</p><h1>JD.com Announces Second Quarter 2026 Results</h1>" + b"<p>x</p>" * 20000 + b"</body></html>"
)
OTHER_HTML = b"<html><body><h1>Notice of Annual General Meeting</h1>" + b"<p>x</p>" * 20000 + b"</body></html>"


def test_headline():
    assert headline(RESULTS_HTML) == "JD.com Announces Second Quarter 2026 Results"
    assert headline(OTHER_HTML) is None


HK_DISCLAIMER = (
    "Hong Kong Exchanges and Clearing Limited and The Stock Exchange of Hong Kong Limited take no responsibility for the contents "
    "of this announcement, make no representation as to its accuracy or completeness and expressly disclaim any liability whatsoever. "
) * 8  # long enough to push the title past the first 1,500 characters, as in Alibaba's September quarter 2025 announcement


def hk_announcement(title: str) -> bytes:
    body = f"<p>EX-99.1 Exhibit 99.1</p><p>{HK_DISCLAIMER}</p><p>Alibaba Group Holding Limited (Stock Code: 9988)</p><p>{title}</p>"
    body += "<p>The board of directors is pleased to announce that the unaudited consolidated results of the company.</p>"
    return f"<html><body>{body}</body></html>".encode() + b"<p>x</p>" * 20000


def test_headline_of_a_hong_kong_style_results_announcement():
    interim = "ANNOUNCEMENT OF THE SEPTEMBER QUARTER 2025 RESULTS AND INTERIM RESULTS FOR THE SIX MONTHS ENDED SEPTEMBER 30, 2025"
    annual = "ANNOUNCEMENT OF THE MARCH QUARTER 2025 RESULTS AND FISCAL YEAR 2025 ANNUAL RESULTS"
    assert headline(hk_announcement(interim)) == interim
    assert headline(hk_announcement(annual)) == annual
    assert headline(hk_announcement("ANNOUNCEMENT OF THE JUNE QUARTER 2026 RESULTS")) == "ANNOUNCEMENT OF THE JUNE QUARTER 2026 RESULTS"
    assert headline(hk_announcement("VOLUNTARY ANNOUNCEMENT SHARE REPURCHASE UPDATE")) is None  # other announcements are not results


def test_acceptance_time_is_converted_to_new_york_time():
    assert to_new_york("2026-08-13T10:05:29.000Z") == pd.Timestamp("2026-08-13 06:05:29")  # daylight saving time
    assert to_new_york("2026-01-13T21:05:29.000Z") == pd.Timestamp("2026-01-13 16:05:29")  # standard time


def test_missing_user_agent_is_reported(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(SecUserAgentMissing, match="SEC_USER_AGENT"):
        make_http_client()


SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["6-K", "6-K", "20-F"],
            "accessionNumber": ["0001193125-26-000001", "0001193125-26-000002", "0001193125-26-000003"],
            "acceptanceDateTime": ["2026-08-13T10:05:29.000Z", "2026-09-01T10:00:00.000Z", "2026-04-01T10:00:00.000Z"],
        },
        "files": [],
    }
}


def listing(*files):
    return json.dumps({"directory": {"item": [{"name": n, "size": str(s)} for n, s in files]}}).encode()


class FakeHttp:
    def __init__(self):
        self.requests = []

    def get_json(self, url, params=None):
        self.requests.append(url)
        return SUBMISSIONS

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        if url.endswith("/000119312526000001/index.json"):
            return listing(("a6k.htm", 8000), ("ex991.htm", 300_000))
        if url.endswith("/000119312526000002/index.json"):
            return listing(("b6k.htm", 8000), ("ex991.htm", 20_000_000))  # exhibits that are too large are not downloaded
        if url.endswith("/000119312526000001/ex991.htm"):
            return RESULTS_HTML
        raise AssertionError(url)


def test_provider_keeps_results_and_remembers_what_it_has_seen(tmp_path):
    http = FakeHttp()
    provider = SecProvider(http, tmp_path)
    found = provider.results_announcements("JD", 1549802, datetime(2026, 1, 1))
    assert [(f.title, f.published) for f in found] == [("JD.com Announces Second Quarter 2026 Results", pd.Timestamp("2026-08-13 06:05:29"))]
    assert found[0].content == RESULTS_HTML
    index = json.loads((tmp_path / "JD" / "index.json").read_text(encoding="utf-8"))
    assert [(e["accession"], e["results"]) for e in index] == [("0001193125-26-000001", True), ("0001193125-26-000002", False)]  # the 20-F is not a 6-K

    n = len(http.requests)
    provider.results_announcements("JD", 1549802, datetime(2026, 1, 1))
    assert len(http.requests) == n + 1  # submissions fetched only once more; 6-Ks already seen are not checked or downloaded again


def test_provider_works_offline_from_the_archive(tmp_path):
    SecProvider(FakeHttp(), tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1))
    offline = SecProvider(None, tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1))
    assert [f.title for f in offline] == ["JD.com Announces Second Quarter 2026 Results"]
    assert SecProvider(None, tmp_path).results_announcements("JD", 1549802, datetime(2026, 9, 1)) == []
    with pytest.raises(SecUserAgentMissing):
        SecProvider(None, tmp_path / "empty").results_announcements("JD", 1549802, datetime(2026, 1, 1))


@pytest.mark.parametrize(
    "text, expected",
    [
        ("EX-99.1 Exhibit 99.1 Meta Reports Third Quarter 2026 Results", "Meta Reports Third Quarter 2026 Results"),
        ("Cognizant Reports Fourth Quarter and Full Year 2025 Results", "Cognizant Reports Fourth Quarter and Full Year 2025 Results"),
        ("ASML reports EUR 2.6 billion sales at 43.0% gross margin in Q2 Stronger Logic compensates for Memory weakness VELDHOVEN, July 17, 2019 - today ASML Holding N.V. (ASML) publishes its 2019 second-quarter results.", None),
        ("Notice of Annual General Meeting", None),
    ],
)
def test_headline_for_other_companies(text, expected):
    html = f"<html><body><p>{text}</p>".encode() + b"<p>x</p>" * 5000 + b"</body></html>"
    found = headline(html)
    if expected is None and "ASML" not in text:
        assert found is None
    elif expected is None:
        assert found is not None and "second-quarter results" in found  # "today ... publishes its 2019 second-quarter results" counts as a results announcement
    else:
        assert found == expected


SUBMISSIONS_8K = {
    "filings": {
        "recent": {
            "form": ["8-K", "8-K", "6-K"],
            "items": ["2.02,9.01", "5.02,9.01", ""],
            "accessionNumber": ["0001326801-26-000001", "0001326801-26-000002", "0001326801-26-000003"],
            "acceptanceDateTime": ["2026-07-29T20:03:23.000Z", "2026-08-02T20:00:00.000Z", "2026-08-03T20:00:00.000Z"],
        },
        "files": [],
    }
}
RELEASE_8K = b"<html><body><p>EX-99.1 Exhibit 99.1</p><h1>Meta Reports Second Quarter 2026 Results</h1>" + b"<p>x</p>" * 20000 + b"</body></html>"


class Fake8kHttp:
    def __init__(self):
        self.requests = []

    def get_json(self, url, params=None):
        return SUBMISSIONS_8K

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        if url.endswith("/000132680126000001/index.json"):
            return listing(("meta-20260729.htm", 40_000), ("meta-06302026xexhibit991.htm", 300_000))
        if url.endswith("/000132680126000001/meta-06302026xexhibit991.htm"):
            return RELEASE_8K
        raise AssertionError(f"unexpected request: {url}")  # the 8-K without item 2.02 and the 6-K must not be looked at


def test_an_8k_counts_only_with_item_2_02_and_exhibit_names_like_exhibit991_are_found(tmp_path):
    http = Fake8kHttp()
    found = SecProvider(http, tmp_path).results_announcements("META", 1326801, datetime(2026, 1, 1), "8-K")
    assert [(f.title, f.published) for f in found] == [("Meta Reports Second Quarter 2026 Results", pd.Timestamp("2026-07-29 16:03:23"))]
    assert len(http.requests) == 2  # the file list of the one earnings 8-K and its exhibit: nothing else was opened


STATEMENTS_HTML = b"<html><body><p>EX-99.3 4 financial_statementsxusx.htm EX-99.3 Exhibit 99.3 ASML - Summary US GAAP Consolidated Statements of Operations</p>" + b"<p>x</p>" * 20000 + b"</body></html>"
IFRS_HTML = b"<html><body><p>EX-99.5 6 d854985dex995.htm EX-99.5 Exhibit 99.5 ASML - Summary IFRS Consolidated Statement of Profit or Loss</p>" + b"<p>x</p>" * 20000 + b"</body></html>"


def test_statements_title_tells_the_us_gaap_summary_from_the_ifrs_one_and_a_press_release():
    from valuation_monitor.providers.sec import statements_title

    assert statements_title(STATEMENTS_HTML) == "Summary US GAAP financial statements"
    assert statements_title(IFRS_HTML) is None
    assert statements_title(RELEASE_8K) is None
    assert statements_title(b"<html><body><p>ASML reports 6.2 billion sales. Reconciliation to US GAAP Consolidated Statements of Operations is in exhibit 99.3</p></body></html>" + b"x" * 40000) is None


class FakeStatementsHttp:
    def __init__(self):
        self.requests = []

    def get_json(self, url, params=None):
        return {"filings": {"recent": {"form": ["6-K"], "accessionNumber": ["0000937966-26-000001"], "acceptanceDateTime": ["2026-10-14T10:01:18.000Z"]}, "files": []}}

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        if url.endswith("/index.json"):
            return listing(("form6-kquarterlyfilings.htm", 11_000), ("pressreleasefinancialresul.htm", 41_000), ("financial_statementsxusx.htm", 28_000), ("presentationinvestorrela.htm", 20_600))
        if url.endswith("financial_statementsxusx.htm"):
            return STATEMENTS_HTML
        raise AssertionError(f"unexpected request: {url}")  # the press release, the presentation and the form itself are not downloaded


def test_the_statements_mode_keeps_the_us_gaap_summary_not_the_press_release(tmp_path):
    http = FakeStatementsHttp()
    found = SecProvider(http, tmp_path).results_announcements("ASML", 937966, datetime(2026, 1, 1), "6-K", "us_gaap_statements")
    assert [(f.title, f.url.rsplit("/", 1)[-1]) for f in found] == [("Summary US GAAP financial statements", "financial_statementsxusx.htm")]
    assert found[0].content == STATEMENTS_HTML and found[0].published == pd.Timestamp("2026-10-14 06:01:18")


# ---- XP: IFRS consolidated financial statements furnished on 6-K ---------------------------------------------------------------------------

NUMBER_TABLE = "<table>" + "<tr><td>Total revenue and income</td><td>5,157,533</td><td>4,660,687</td></tr>" * 60 + "</table>"


def xp_exhibit(start: str, tables: str = NUMBER_TABLE) -> bytes:
    return f"<html><body><p>EX-99.1 2 dp256944_ex9901.htm EXHIBIT 99.1 Exhibit 99.1</p><p>{start}</p>{tables}</body></html>".encode()


IFRS_STATEMENTS = xp_exhibit("XP Inc. Interim condensed consolidated financial statements at September 30, 2026 and report on review")


@pytest.mark.parametrize(
    "start, title",
    [
        ("XP Inc. Interim condensed consolidated financial statements at September 30, 2026 and report on review", "Interim condensed consolidated financial statements at September 30, 2026"),
        ("www.pwc.com.br XP Inc. Consolidated financial statements at December 31, 2021 and independent auditor's report", "Consolidated financial statements at December 31, 2021"),
        ("XP Inc. XP Inc. Consolidated financial statements at December 31, 2024 and independent auditor's report", "Consolidated financial statements at December 31, 2024"),
        ("XP Inc. and its subsidiaries Unaudited interim condensed consolidated balance sheets As of March 31, 2021 and December 31, 2020", "Interim condensed consolidated financial statements at March 31, 2021"),
        ("Index Report on review of interim condensed consolidated financial statements 3 Unaudited interim condensed consolidated balance sheets As of March 31, 2020", "Interim condensed consolidated financial statements at March 31, 2020"),
    ],
)
def test_ifrs_statements_are_known_by_their_heading_the_report_on_them_or_the_balance_sheet(start, title):
    from valuation_monitor.providers.sec import ifrs_statements_title

    assert ifrs_statements_title(xp_exhibit(start)) == title


def test_a_press_release_a_discussion_or_page_images_are_not_ifrs_statements():
    from valuation_monitor.providers.sec import ifrs_statements_title

    assert ifrs_statements_title(xp_exhibit("XP Inc. Reports Third Quarter 2026 Results São Paulo, Brazil, November 16, 2026 – XP Inc. reported today " + "x " * 60 + "interim condensed consolidated financial statements at September 30, 2026")) is None
    assert ifrs_statements_title(xp_exhibit("Management's Discussion and Analysis of Financial Condition and Results of Operations The following discussion of our financial condition and results of operations should be read in conjunction with our unaudited interim condensed consolidated financial statements as of March 31, 2020")) is None
    # the 2019 annual statements are page images with a garbled hidden text layer and no table of amounts
    assert ifrs_statements_title(xp_exhibit("XP Inc. – Consolidated financial statements for the years ended December 31, 2019, 2018 and 2017 with Audit Report", "<p>Total revenue and income 5 , 12 7 , 8 0 8</p>")) is None


class FakeIfrsHttp:
    def __init__(self):
        self.requests = []

    def get_json(self, url, params=None):
        return {"filings": {"recent": {"form": ["6-K", "6-K"], "accessionNumber": ["0000950103-26-017561", "0000950103-26-017562"],
                                       "acceptanceDateTime": ["2026-11-16T21:13:00.000Z", "2026-11-16T21:14:19.000Z"]}, "files": []}}

    def get_bytes(self, url, **kwargs):
        self.requests.append(url)
        if url.endswith("000095010326017561/index.json"):  # the press release and the presentation
            return listing(("dp256943_6k-er.htm", 11_990), ("dp256943_ex9901.htm", 248_957), ("dp256943_ex9902.htm", 29_011))
        if url.endswith("000095010326017562/index.json"):  # the statements, in a 6-K of their own, larger than a press release may be
            return listing(("dp256944_6k-fs.htm", 11_221), ("dp256944_ex9901.htm", 4_498_362))
        if url.endswith("dp256943_ex9901.htm"):
            return xp_exhibit("XP Inc. Reports Third Quarter 2026 Results São Paulo, Brazil, November 16, 2026")
        if url.endswith("dp256943_ex9902.htm"):
            return xp_exhibit("3Q26 Earnings Presentation", "")
        if url.endswith("dp256944_ex9901.htm"):
            return IFRS_STATEMENTS
        raise AssertionError(f"unexpected request: {url}")


def test_the_ifrs_mode_keeps_the_statements_6k_and_skips_the_press_release(tmp_path):
    found = SecProvider(FakeIfrsHttp(), tmp_path).results_announcements("XP", 1787425, datetime(2026, 1, 1), "6-K", "ifrs_statements")
    assert [(f.title, f.url.rsplit("/", 1)[-1]) for f in found] == [("Interim condensed consolidated financial statements at September 30, 2026", "dp256944_ex9901.htm")]
    assert found[0].content == IFRS_STATEMENTS and found[0].published == pd.Timestamp("2026-11-16 16:14:19")
    index = json.loads((tmp_path / "XP" / "index.json").read_text(encoding="utf-8"))
    assert [e["results"] for e in index] == [False, True]  # the press release 6-K is recorded as checked, not as results


# ---- EDGAR's CDN answering 404 "Directory listing is Not Available Yet" for a while ----------------------------------------------------------


def not_found(url, status=404):
    import requests

    response = requests.Response()
    response.status_code = status
    response.url = url
    return requests.HTTPError(f"{status} Client Error: for url: {url}", response=response)


INDEX_PAGE = b"""<html><body><table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>6-K</td><td><a href="/Archives/edgar/data/1549802/000119312526000001/a6k.htm">a6k.htm</a></td><td>6-K</td><td>8000</td></tr>
<tr><td>2</td><td>EX-99.1</td><td><a href="/Archives/edgar/data/1549802/000119312526000001/ex991.htm">ex991.htm</a></td><td>EX-99.1</td><td>300000</td></tr>
<tr><td></td><td>Complete submission text file</td><td><a href="/Archives/edgar/data/1549802/000119312526000001/0001193125-26-000001.txt">0001193125-26-000001.txt</a></td><td></td><td>400000</td></tr>
</table></body></html>"""


class FlakyHttp(FakeHttp):
    """The index.json of the first 6-K answers 404 (the CDN's cached page); its index page and exhibit answer `page_failures` times 404 too."""

    def __init__(self, page_failures=0, status=404):
        super().__init__()
        self.page_failures, self.status = page_failures, status
        self.calls = []

    def get_bytes(self, url, **kwargs):
        self.calls.append(url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1])
        if url.endswith("/000119312526000001/index.json"):
            raise not_found(url, self.status)
        if url.endswith("/000119312526000001/0001193125-26-000001-index.htm"):
            if self.page_failures:
                self.page_failures -= 1
                raise not_found(url)
            return INDEX_PAGE
        return super().get_bytes(url, **kwargs)


def test_the_index_page_lists_the_documents_like_index_json():
    from valuation_monitor.providers.sec import index_page_items

    assert index_page_items(INDEX_PAGE) == [{"name": "0001193125-26-000001.txt", "size": "400000"}, {"name": "a6k.htm", "size": "8000"}, {"name": "ex991.htm", "size": "300000"}]


def test_when_index_json_answers_404_the_filing_index_page_is_used(tmp_path):
    http = FlakyHttp()
    found = SecProvider(http, tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1))
    assert [f.title for f in found] == ["JD.com Announces Second Quarter 2026 Results"]
    assert http.calls[:3] == ["000119312526000001/index.json", "000119312526000001/0001193125-26-000001-index.htm", "000119312526000001/ex991.htm"]
    index = json.loads((tmp_path / "JD" / "index.json").read_text(encoding="utf-8"))
    assert [(e["accession"], e["results"]) for e in index] == [("0001193125-26-000001", True), ("0001193125-26-000002", False)]


def test_a_filing_answering_404_is_tried_again_after_the_others(tmp_path):
    http = FlakyHttp(page_failures=1)  # the first pass fails, the second pass works
    found = SecProvider(http, tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1))
    assert [f.title for f in found] == ["JD.com Announces Second Quarter 2026 Results"]
    listings = [c for c in http.calls if "index" in c]
    assert [c.split("/")[0][-1] for c in listings] == ["1", "1", "2", "1", "1"]  # the second 6-K is checked before the first one is tried again


def test_a_filing_that_stays_404_is_skipped_for_this_run_only_and_the_others_go_on(tmp_path, capsys):
    assert SecProvider(FlakyHttp(page_failures=2), tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1)) == []
    index = json.loads((tmp_path / "JD" / "index.json").read_text(encoding="utf-8"))
    assert [e["accession"] for e in index] == ["0001193125-26-000002"]  # the other 6-K was still checked; the skipped one is not recorded
    assert "skipping the filing 0001193125-26-000001 for this run" in capsys.readouterr().err

    found = SecProvider(FakeHttp(), tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1))  # the next run checks it again
    assert [f.title for f in found] == ["JD.com Announces Second Quarter 2026 Results"]


def test_other_http_errors_still_stop_the_update_as_before(tmp_path):
    import requests

    with pytest.raises(requests.HTTPError):
        SecProvider(FlakyHttp(status=403), tmp_path).results_announcements("JD", 1549802, datetime(2026, 1, 1))
