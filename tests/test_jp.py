"""Japan (SHIFT Inc., 3697.T): the Xj-Storage IR library list, the Kessan Tanshin parser, corrections and the fiscal year ending in August,
verified with fragments of the original Japanese filings and with the saved filings. The Japanese strings in this file are fixtures:
titles and text of the Japanese-language source filings."""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor import jp_tanshin
from valuation_monitor.config import load_config, parse_config
from valuation_monitor.filings import Announcement, PERIOD_END_SHARES, announcements_from_frame, announcements_to_frame, build_history, fiscal_quarter, parse_announcement
from valuation_monitor.jp_tanshin import english_title, normalize, parse_jp_tanshin, share_count, to_number
from valuation_monitor.market_calendar import JP
from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.xj_storage import XjStorageProvider, classify, period_end

ROOT = Path(__file__).parent.parent
FOLDER = ROOT / "data" / "filings" / "3697.T"
saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved SHIFT filings (the first scheduled update saves them)")
Q = lambda s: pd.Period(s, freq="Q")  # noqa: E731

# The summary of the FY2026 first-quarter Kessan Tanshin (published 2026-01-14), as read_text gives it
Q1_FY2026 = normalize(
    "１．2026年８月期第１四半期の連結業績（2025年９月１日～2025年11月30日） （１）連結経営成績(累計) (％表示は、対前年同四半期増減率) "
    "売上高 営業利益 経常利益 親会社株主に帰属 する四半期純利益 百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％ "
    "2026年８月期第１四半期 34,845 15.5 2,817 △19.9 2,778 △16.2 1,795 △9.2 2025年８月期第１四半期 30,174 20.3 3,517 94.0 3,313 76.0 1,976 80.7 "
    "(注)包括利益 2026年８月期第１四半期 1,743百万円( △14.1％) 2025年８月期第１四半期 2,029百万円( 92.2％) "
    "１株当たり 四半期純利益 潜在株式調整後 １株当たり 四半期純利益 円 銭 円 銭 2026年８月期第１四半期 6.82 6.81 2025年８月期第１四半期 7.50 7.49 "
    "（２）連結財政状態 総資産 純資産 自己資本比率 百万円 百万円 ％ 2026年８月期第１四半期 79,102 42,862 53.5 2025年８月期 77,001 41,037 52.7 "
    "(参考) 自己資本 2026年８月期第１四半期 42,347百万円 2025年８月期 40,598百万円 "
    "（４）発行済株式数（普通株式） ① 期末発行済株式数（自己株式を含む) 2026年８月期１Ｑ 267,500,670株 2025年８月期 267,500,670株 "
    "② 期末自己株式数 2026年８月期１Ｑ 4,208,457株 2025年８月期 4,215,507株"
)
# The FY2016 second quarter (Heisei 28): EPS printed as yen and sen apart, no treasury shares a year earlier
Q2_FY2016 = normalize(
    "（１）連結経営成績(累計) 売上高営業利益経常利益親会社株主に帰属する四半期純利益百万円 % 百万円 % 百万円 % 百万円 % "
    "28年8月期第2四半期 2,512 77.2 202 20.6 206 26.9 130 24.7 27年8月期第2四半期 1,418 ― 167 ― 163 ― 104 ― "
    "(注) 包括利益 28年8月期第2四半期 132 百万円 (23.3%) 27年8月期第2四半期 107 百万円 ( ―%) "
    "1株当たり四半期純利益潜在株式調整後 1株当たり四半期純利益円銭円銭 28年8月期第2四半期 9 06 8 37 27年8月期第2四半期 7 73 6 88 "
    "(参考) 自己資本 28年8月期第2四半期 1,557 百万円 27年8月期 1,489 百万円 "
    "期末発行済株式数(自己株式を含む) 28年8月期2Q 14,435,500株 27年8月期 14,410,500株 期末自己株式数 28年8月期2Q 78,600株 27年8月期 -株"
)
# The first quarter after the listing (FY2015, Heisei 27): no prior-year figures
Q1_FY2015 = normalize(
    "（１）連結経営成績(累計) 売上高営業利益経常利益四半期純利益百万円 % 百万円 % 百万円 % 百万円 % 27年8月期第1四半期 658 ― 85 ― 78 ― 50 ― "
    "26年8月期第1四半期 ― ― ― ― ― ― ― ― (注) 包括利益 27年8月期第1四半期 53 百万円 ( ―%) 26年8月期第1四半期 ― 百万円 ( ―%) "
    "1株当たり四半期純利益潜在株式調整後 1株当たり四半期純利益円銭円銭 27年8月期第1四半期 19.64 17.03 26年8月期第1四半期 ― ― "
    "(参考) 自己資本 27年8月期第1四半期 1,239 百万円 26年8月期 1,056 百万円 "
    "期末発行済株式数(自己株式を含む) 27年8月期1Q 2,761,600株 26年8月期 2,513,600株 期末自己株式数 27年8月期1Q ―株 26年8月期 ―株"
)
# The interim period of FY2025, so labelled since FY2025, and the full year
H1_FY2025 = normalize(
    "連結経営成績(累計) 売上高 営業利益 経常利益 親会社株主に帰属する中間純利益 百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％ "
    "2025年８月期中間期 61,688 17.9 8,058 72.1 7,868 62.9 4,493 50.7 2024年８月期中間期 52,305 29.6 4,685 △7.8 4,638 △9.6 2,982 36.3 "
    "１株当たり 中間純利益 円 銭 円 銭 2025年８月期中間期 17.06 17.05 2024年８月期中間期 11.31 11.30 "
    "(参考) 自己資本 2025年８月期中間期 35,814百万円 2024年８月期 33,684百万円 "
    "① 期末発行済株式数（自己株式を含む) 2025年８月期中間期 267,500,670株 2024年８月期 267,500,670株 ② 期末自己株式数 2025年８月期中間期 4,303,665株 2024年８月期 5,105,610株"
)
FY2025 = normalize(
    "（１）連結経営成績 (％表示は対前期増減率) 売上高 営業利益 経常利益 親会社株主に帰属する 当期純利益 百万円 ％ 百万円 ％ 百万円 ％ 百万円 ％ "
    "2025年８月期 129,819 17.3 15,628 48.3 15,181 41.2 8,935 74.3 2024年８月期 110,627 25.7 10,537 △8.9 10,753 △10.4 5,127 △17.9 "
    "(注) 包括利益 2025年８月期 9,365百万円( 94.4％) 2024年８月期 4,817百万円( △25.0％) "
    "１株当たり 当期純利益 潜在株式調整後 １株当たり 当期純利益 自己資本 当期純利益率 総資産 経常利益率 売上高 営業利益率 円 銭 円 銭 ％ ％ ％ "
    "2025年８月期 33.93 33.90 24.1 21.7 12.0 2024年８月期 19.43 19.41 16.4 19.2 9.5 "
    "(参考) 自己資本 2025年８月期 40,598百万円 2024年８月期 33,684百万円 "
    "（注）期末発行済株式数の算出に当たり、「株式給付型ESOP信託口」が保有する当社株式の期末株式数を控除する自己株式に含めております。 "
    "① 期末発行済株式数（自己株式を含む) 2025年８月期 267,500,670株 2024年８月期 267,500,670株 ② 期末自己株式数 2025年８月期 4,215,507株 2024年８月期 3,407,040株"
)


def parse_text(monkeypatch, text: str, title: str):
    monkeypatch.setattr(jp_tanshin, "read_text", lambda pdf: text)
    return parse_jp_tanshin(title, b"")


def test_classify_titles():
    assert classify("2026年８月期　第１四半期決算短信〔日本基準〕(連結)") == (2026, 1, 8, False)
    assert classify("2025年８月期　第２四半期（中間期）決算短信〔日本基準〕(連結)") == (2025, 2, 8, False)
    assert classify("平成27年８月期 第１四半期決算短信〔日本基準〕（連結）") == (2015, 1, 8, False)
    assert classify("平成27年８月期　決算短信〔日本基準〕（連結）") == (2015, 4, 8, False)
    assert classify("（訂正・数値データ訂正) 「2024年８月期　決算短信〔日本基準〕（連結）」の一部訂正について") == (2024, 4, 8, True)
    for skipped in ("2025年８月期　第２四半期決算説明資料", "2026年８月期　第１四半期決算説明資料", "株主総会招集通知", "Consolidated Financial Results for the Year Ended August 31, 2026 [Japanese GAAP]"):
        assert classify(skipped) is None


def test_period_ends_and_english_titles():
    assert period_end(2026, 1, 8) == pd.Timestamp("2025-11-30")
    assert period_end(2024, 2, 8) == pd.Timestamp("2024-02-29")
    assert period_end(2026, 4, 8) == pd.Timestamp("2026-08-31")
    assert english_title(2026, 1, pd.Timestamp("2025-11-30")) == "FY2026 Q1 Kessan Tanshin (consolidated results for the three months ended November 30, 2025)"
    assert english_title(2024, 4, pd.Timestamp("2024-08-31"), True) == "Correction of the FY2024 Kessan Tanshin (consolidated results for the year ended August 31, 2024)"
    # internally each fiscal quarter is kept under the calendar quarter holding its end (and two of its months)
    assert [str(pd.Period(period_end(2026, q, 8), freq="Q")) for q in (1, 2, 3, 4)] == ["2025Q4", "2026Q1", "2026Q2", "2026Q3"]


def test_cells():
    assert to_number("△19.9") == -19.9 and to_number("7 73") == 7.73 and to_number("1,795") == 1795.0 and to_number("―") is None
    assert share_count("66.260") == 66260 and share_count("4,208,457") == 4208457 and share_count("―") == 0


def test_first_quarter(monkeypatch):
    p = parse_text(monkeypatch, Q1_FY2026, english_title(2026, 1, pd.Timestamp("2025-11-30")))
    assert (p.currency, p.period, p.quarterly) == ("JPY", Q("2025Q4"), True)
    assert p.values == {"revenue": [34845.0, 30174.0], "profit": [1795.0, 1976.0], "eps": [6.82, 7.50], "equity": [42347.0], "wa_shares": [263.292213]}


def test_heisei_labels_and_yen_sen_eps(monkeypatch):
    p = parse_text(monkeypatch, Q2_FY2016, english_title(2016, 2, pd.Timestamp("2016-02-29")))
    assert (p.period, p.quarterly) == (Q("2016Q1"), False)
    assert p.values["revenue"] == [2512.0, 1418.0] and p.values["profit"] == [130.0, 104.0] and p.values["eps"] == [9.06, 7.73]
    assert p.values["equity"] == [1557.0] and p.values["wa_shares"] == [pytest.approx(14.3569)]


def test_no_prior_year_figures_after_the_listing(monkeypatch):
    p = parse_text(monkeypatch, Q1_FY2015, english_title(2015, 1, pd.Timestamp("2014-11-30")))
    assert p.values == {"revenue": [658.0], "profit": [50.0], "eps": [19.64], "equity": [1239.0], "wa_shares": [2.7616]}


def test_interim_label_and_full_year(monkeypatch):
    p = parse_text(monkeypatch, H1_FY2025, english_title(2025, 2, pd.Timestamp("2025-02-28")))
    assert p.values["eps"] == [17.06, 11.31] and p.values["wa_shares"] == [pytest.approx(263.197005)]
    p = parse_text(monkeypatch, FY2025, english_title(2025, 4, pd.Timestamp("2025-08-31")))
    assert (p.period, p.quarterly) == (Q("2025Q3"), False)
    assert p.values["revenue"] == [129819.0, 110627.0] and p.values["eps"] == [33.93, 19.43] and p.values["equity"] == [40598.0]
    assert p.values["wa_shares"] == [pytest.approx(263.285163)]  # the note on the period-end shares before the counts is not a count


def test_the_title_must_match_the_document(monkeypatch):
    with pytest.raises(ValueError, match="Row not found"):
        parse_text(monkeypatch, Q1_FY2026, english_title(2025, 1, pd.Timestamp("2024-11-30")))
    with pytest.raises(ValueError, match="Row not found"):
        parse_text(monkeypatch, Q1_FY2026, english_title(2026, 2, pd.Timestamp("2026-02-28")))
    with pytest.raises(ValueError, match="title"):
        parse_jp_tanshin("FY2026 Q1 results", b"")


def test_config_for_a_japanese_stock():
    s = parse_config({"stocks": [{"symbol": "3697.T", "name": "SHIFT", "metrics": ["pe"], "fiscal_year_end": 8}]}).stocks[0]
    assert (s.market, s.filing_format, s.cik, s.fiscal_year_end) == ("JP", "jp_tanshin", None, 8) and "Xj-Storage" in s.filing_source
    assert not s.cross_checks_prices
    with pytest.raises(ValueError, match="does not match"):
        parse_config({"stocks": [{"symbol": "3697.T", "filing_format": "cn_report"}]})
    assert parse_config({"stocks": [{"symbol": "3697.T", "name": "SHIFT"}]}).freshness.max_price_age_days["JP"] == 6


def test_shift_is_configured():
    stock = next(s for s in load_config(ROOT / "config.yaml").stocks if s.symbol == "3697.T")
    assert (stock.name, stock.market, stock.filing_format, stock.fiscal_year_end, stock.metrics) == ("SHIFT", "JP", "jp_tanshin", 8, ["pe", "ps", "pb"])


# ---- the provider ------------------------------------------------------------------------------------------------------------

def listing(*items):
    return json.dumps({"length": str(len(items)), "items": [
        {"title": t, "publishDate": d, "disclosureCode": c, "files": [{"type": "PDF-GENERAL", "url": f"https://contents.xj-storage.jp/x/{u}.pdf"}]}
        for t, d, c, u in items
    ]}, ensure_ascii=False).encode()


class FakeHttp:
    def __init__(self, items):
        self.items = items
        self.fetched = []

    def get_bytes(self, url, params=None, cache=True, **kw):
        if "GetList" in url:
            assert params["company"] == "AS95685"
            return self.items
        self.fetched.append(url.rsplit("/", 1)[-1])
        return url.rsplit("/", 1)[-1].encode()


def test_provider_lists_tanshin_and_corrections(tmp_path, monkeypatch):
    monkeypatch.setattr("valuation_monitor.providers.xj_storage.excerpt_pages", lambda pdf: pdf)
    monkeypatch.setattr("valuation_monitor.providers.xj_storage.page_texts", lambda pdf: [pdf.decode()])
    monkeypatch.setattr("valuation_monitor.providers.xj_storage.has_summary", lambda text: "nocf" not in text)
    http = FakeHttp(listing(
        ("2024年８月期　決算短信〔日本基準〕(連結)", "2024/10/10 15:00:00", "3", "fy24"),
        ("（訂正・数値データ訂正) 「2024年８月期　決算短信〔日本基準〕（連結）」の一部訂正について", "2024/10/30 15:00:00", "20", "fy24c"),
        ("（訂正・数値データ訂正) 「2023年８月期　決算短信〔日本基準〕（連結）」の一部訂正について", "2023/10/24 15:45:00", "3", "fy23c-dup"),
        ("（訂正・数値データ訂正) 「2023年８月期　決算短信〔日本基準〕（連結）」の一部訂正について", "2023/10/24 15:45:00", "25", "fy23c"),
        ("（訂正・数値データ訂正）「2021年8月期　決算短信〔日本基準〕（連結）」の一部訂正について", "2021/11/22 16:15:00", "25", "nocf"),
        ("2025年８月期　第１四半期決算短信〔日本基準〕(連結)", "2025/01/14 15:30:00", "16", "q1"),
        ("2025年８月期　第１四半期決算説明資料", "2025/01/14 15:30:00", "16", "deck"),
    ))
    provider = XjStorageProvider(http, tmp_path)
    filings = provider.results_announcements("3697.T", datetime(2021, 1, 1))
    assert [(f.title, f"{f.published}", f.correction) for f in filings] == [
        ("Correction of the FY2023 Kessan Tanshin (consolidated results for the year ended August 31, 2023)", "2023-10-24 15:45:00", True),
        ("FY2024 Kessan Tanshin (consolidated results for the year ended August 31, 2024)", "2024-10-10 15:00:00", False),
        ("Correction of the FY2024 Kessan Tanshin (consolidated results for the year ended August 31, 2024)", "2024-10-30 15:00:00", True),
        ("FY2025 Q1 Kessan Tanshin (consolidated results for the three months ended November 30, 2024)", "2025-01-14 15:30:00", False),
    ]
    assert "fy23c-dup.pdf" not in http.fetched and "deck.pdf" not in http.fetched  # the numerical data correction is taken; presentations are not filings
    assert "2024-10-10 15:00" in filings[2].note
    index = json.loads((tmp_path / "3697.T" / "index.json").read_text(encoding="utf-8"))
    unused = next(e for e in index if e["url"].endswith("nocf.pdf"))
    assert unused["file"] is None and "does not read" in unused["note"]  # a correction of the cash flows only: recorded, not saved
    assert all(e["basis"].startswith("Xj-Storage listing time") for e in index)
    # nothing new: nothing fetched again; and unreachable: the saved filings are used
    fetched = len(http.fetched)
    assert len(provider.results_announcements("3697.T", datetime(2021, 1, 1))) == 4 and len(http.fetched) == fetched
    provider.http = None
    assert len(provider.results_announcements("3697.T", datetime(2021, 1, 1))) == 4
    assert provider.release_info("3697.T") == {}


# ---- the history: a fiscal year ending in August, and corrections ---------------------------------------------------------

def announcement(period, published, values, quarterly, url, correction=False):
    return Announcement(Q(period), pd.Timestamp(published), url, "JPY", quarterly, values, "JP", shares_label=PERIOD_END_SHARES, correction=correction)


def fiscal_2024():
    """SHIFT's FY2024 (September 2023 to August 2024): the first quarter, then six months, nine months and the full year to date."""
    return [
        announcement("2023Q4", "2024-01-12 15:00", {"revenue": [25073.0], "profit": [1095.0], "eps": [62.3], "equity": [29993.0], "wa_shares": [17.6]}, True, "q1"),
        announcement("2024Q1", "2024-04-11 15:00", {"revenue": [52305.0], "profit": [2977.0], "eps": [169.3], "equity": [31763.0], "wa_shares": [17.6]}, False, "h1"),
        announcement("2024Q2", "2024-07-10 15:00", {"revenue": [81143.0], "profit": [3295.0], "eps": [187.32], "equity": [32075.0], "wa_shares": [17.6]}, False, "9m"),
        announcement("2024Q3", "2024-10-10 15:00", {"revenue": [110627.0], "profit": [5715.0], "eps": [324.82], "equity": [34271.0], "wa_shares": [17.6]}, False, "fy"),
    ]


def test_year_to_date_figures_run_from_the_start_of_the_fiscal_year():
    assert [fiscal_quarter(Q(p), 8) for p in ("2023Q4", "2024Q1", "2024Q2", "2024Q3")] == [1, 2, 3, 4]
    assert [fiscal_quarter(Q(p)) for p in ("2024Q1", "2024Q4")] == [1, 4]  # December year end: the calendar quarter
    h = build_history(fiscal_2024(), fiscal_year_end=8)
    assert [round(h.quarters["revenue"][Q(p)].value) for p in ("2023Q4", "2024Q1", "2024Q2", "2024Q3")] == [25073, 27232, 28838, 29484]
    assert [h.label(p) for p in sorted(h.quarters["revenue"])] == ["FY2024Q1", "FY2024Q2", "FY2024Q3", "FY2024Q4"]
    assert h.ttm["eps_ttm"].iloc[-1] == pytest.approx(324.82) and h.ttm["revenue_ttm"].iloc[-1] == 110627.0


def test_a_correction_is_used_from_its_own_publication():
    """SHIFT's FY2024 Kessan Tanshin of 2024-10-10 was corrected on 2024-10-30 (a goodwill impairment of JPY 587 million)."""
    corrected = announcement(
        "2024Q3", "2024-10-30 15:00", {"revenue": [110627.0], "profit": [5127.0], "eps": [291.43], "equity": [33684.0], "wa_shares": [17.6]}, False, "fy-c", True
    )
    h = build_history([*fiscal_2024(), corrected], fiscal_year_end=8)
    assert list(h.ttm["published"]) == ["2024-01-12 15:00", "2024-04-11 15:00", "2024-07-10 15:00", "2024-10-10 15:00", "2024-10-30 15:00"]
    assert list(h.ttm["eps_ttm"].iloc[-2:]) == [pytest.approx(324.82), pytest.approx(291.43)]  # the original until the correction
    assert list(h.ttm["equity"].iloc[-2:]) == [34271.0, 33684.0] and h.ttm["available_date"].iloc[-1] == pd.Timestamp("2024-10-31")
    q4 = h.quarters["profit"][Q("2024Q3")]
    assert q4.value == 5127.0 - 3295.0 and q4.published == pd.Timestamp("2024-10-30 15:00") and "first published as 2,420" in q4.note
    assert not h.discrepancies()
    assert "corrected" in h.ttm_detail[-1]["eps"][-1].note
    # without the correction the history is what it was
    plain = build_history(fiscal_2024(), fiscal_year_end=8)
    assert plain.ttm.equals(h.ttm.iloc[:-1])
    # a correction that changes no figure adds nothing
    same = announcement("2024Q3", "2024-10-30 15:00", fiscal_2024()[-1].values, False, "fy-same", True)
    assert build_history([*fiscal_2024(), same], fiscal_year_end=8).ttm.equals(plain.ttm)


def test_a_correction_is_stored_and_read_back():
    corrected = announcement("2024Q3", "2024-10-30 15:00", {"revenue": [110627.0], "profit": [5127.0]}, False, "fy-c", True)
    (back,) = announcements_from_frame(announcements_to_frame([corrected], 1).astype(str), JP)
    assert back.correction and not back.is_quarterly_table and back.values == corrected.values
    (plain,) = announcements_from_frame(announcements_to_frame(fiscal_2024()[:1], 1).astype(str), JP)
    assert not plain.correction and plain.is_quarterly_table


def test_a_correction_without_its_original_fails():
    corrected = announcement("2024Q3", "2024-10-30 15:00", {"revenue": [1.0], "profit": [1.0]}, False, "fy-c", True)
    with pytest.raises(ValueError, match="not published before it"):
        build_history([*fiscal_2024()[:3], corrected], fiscal_year_end=8)


def test_parse_announcement_reads_a_correction(monkeypatch):
    monkeypatch.setattr(jp_tanshin, "read_text", lambda pdf: FY2025)
    title = english_title(2025, 4, pd.Timestamp("2025-08-31"), True)
    a = parse_announcement(Filing(title, pd.Timestamp("2025-10-30 15:00"), "u", b"", note="n", correction=True), "jp_tanshin", JP)
    assert a.correction and a.shares_label == PERIOD_END_SHARES and a.market == "JP" and a.note == "n" and a.period == Q("2025Q3")


# ---- the saved filings -------------------------------------------------------------------------------------------------------

@saved
def test_every_saved_filing_parses_and_its_shares_agree_with_profit_over_eps():
    index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    assert len([e for e in index if e["kind"] == "tanshin"]) >= 48
    for e in index:
        if not e["file"]:
            continue
        p = parse_jp_tanshin(e["title"], (FOLDER / e["file"]).read_bytes())
        assert p.period == pd.Period(e["period_end"], freq="Q")
        implied = p.values["profit"][0] / p.values["eps"][0]  # the year-to-date average, against the period-end shares outstanding
        # within 15%: the IPO (November 2014), the 5-for-1 split in February 2015 and the share issues of FY2019 and FY2021 move the end away from the average
        assert abs(implied / p.values["wa_shares"][0] - 1) < 0.15, (e["title"], implied, p.values["wa_shares"])


@saved
def test_saved_fy2026_and_the_2024_correction():
    index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    by_title = {e["title"]: e for e in index}
    fy26 = by_title["FY2026 Kessan Tanshin (consolidated results for the year ended August 31, 2026)"]
    p = parse_jp_tanshin(fy26["title"], (FOLDER / fy26["file"]).read_bytes())
    assert p.values["revenue"] == [161600.0, 129819.0] and p.values["profit"] == [7430.0, 8935.0] and p.values["eps"] == [28.72, 33.93]
    assert p.values["equity"] == [41559.0]
    fix = by_title["Correction of the FY2024 Kessan Tanshin (consolidated results for the year ended August 31, 2024)"]
    assert fix["published"] == "2024-10-30T15:00:00"
    p = parse_jp_tanshin(fix["title"], (FOLDER / fix["file"]).read_bytes())
    assert p.values["profit"] == [5127.0, 6245.0] and p.values["eps"] == [291.43, 354.56] and p.values["equity"] == [33684.0]
