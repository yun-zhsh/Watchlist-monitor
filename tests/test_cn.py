"""A-shares (Hikvision, Glodon, Zhongji Innolight, China Northern Rare Earth): the CNINFO announcement list, publication time rules, and parsing verified with the saved original Chinese reports. The Chinese strings in this file are fixtures: titles and text of the Chinese-language source filings."""

import io
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.cn_report import clean_text, parse_cn_express, parse_cn_report
from valuation_monitor.config import parse_config
from valuation_monitor.market_calendar import CN
from valuation_monitor.providers.cninfo import CninfoProvider, classify, published_time

FOLDER = Path(__file__).parent.parent / "data" / "filings" / "002415.SZ"
saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved Hikvision filings")


def test_classify_titles():
    assert classify("2026年第三季度报告") == ("report", 2026, 3)
    assert classify("2025年三季度报告") == ("report", 2025, 3)
    assert classify("2026年一季度报告") == ("report", 2026, 1)
    assert classify("2014年第一季度报告全文") == ("report", 2014, 1)
    assert classify("2015年第一季度报告全文（更新后）") == ("report", 2015, 1)
    assert classify("2025年半年度报告") == ("report", 2025, 2)
    assert classify("2025年年度报告") == ("report", 2025, 4)
    assert classify("2025年度业绩快报") == ("express", 2025, 4)
    # Shanghai-listed companies put the company name first, and their express report titles end with "announcement"
    assert classify("南京银行股份有限公司2025年年度报告") == ("report", 2025, 4)
    assert classify("南京银行股份有限公司2021年第一季度报告全文") == ("report", 2021, 1)
    assert classify("南京银行股份有限公司2025年度<em>业绩</em><em>快报</em>公告") == ("express", 2025, 4)
    assert classify("2019年半年度业绩快报公告") == ("express", 2019, 2)
    for skipped in ("南京银行股份有限公司2025年年度报告摘要", "2025年年度报告摘要", "2025年年度报告（英文版）", "2025年度业绩快报（英文版）", "2025年环境、社会及治理报告", "2025年第三季度报告更正公告"):
        assert classify(skipped) is None


def test_published_time_prefers_the_upload_time_but_only_after_the_2020_migration():
    listed = pd.Timestamp("2026-10-17 00:00")  # filed Friday 16:00 after the close; CNINFO lists it at 00:00 of the next day
    assert published_time(listed, "Fri, 16 Oct 2026 08:00:19 GMT")[0] == pd.Timestamp("2026-10-16 16:00:19")
    # files migrated in September 2020 have the migration time as upload time, which cannot be used: only the listing date is used
    old = pd.Timestamp("2018-10-20 00:00")
    assert published_time(old, "Sat, 26 Sep 2020 08:25:00 GMT")[0] == old
    assert published_time(old, None)[0] == old
    # an upload time that does not match the listing date (more than one day apart) is not used either
    assert published_time(listed, "Mon, 05 Oct 2026 08:00:00 GMT")[0] == listed


def test_a_share_market_closes_at_1500():
    assert CN.available_date(pd.Timestamp("2026-10-16 16:00")) == pd.Timestamp("2026-10-17")  # Friday after the close: the next trading day (Monday in the price data)
    assert CN.available_date(pd.Timestamp("2026-10-17 00:00")) == pd.Timestamp("2026-10-17")  # date only: treated as before the open
    assert CN.available_date(pd.Timestamp("2026-10-16 14:59")) == pd.Timestamp("2026-10-16")


def test_config_for_a_share():
    s = parse_config({"stocks": [{"symbol": "002415.SZ", "name": "Hikvision", "metrics": ["pe"]}]}).stocks[0]
    assert (s.market, s.filing_format, s.cik) == ("CN", "cn_report", None) and "CNINFO" in s.filing_source and not s.cross_checks_prices
    with pytest.raises(ValueError, match="does not match"):
        parse_config({"stocks": [{"symbol": "002415.SZ", "filing_format": "highlights"}]})


def test_pdf_digit_artifacts_are_repaired():
    assert clean_text("股本 9,335,806,1 14.00 9,343,417,190.00") == "股本 9,335,806,114.00 9,343,417,190.00"
    assert clean_text("净利润 10,965,641,158.1 1 29.94%") == "净利润 10,965,641,158.11 29.94%"
    assert clean_text("归属于上市 公司股东") == "归属于上市公司股东"


class FakeHttp:
    """A fake CNINFO: a Q3 report (published after the close), an annual express report, and later the annual report."""

    def __init__(self):
        self.lists = {}
        self.downloads = []

    def post_json(self, url, data, retries=5):
        if "topSearch" in url:
            return [{"code": "002415", "orgId": "9900012688"}]
        start = data["seDate"][:4]
        rows = [r for r in self.lists.get(data["searchkey"], []) if r["announcementTime"] is not None and (data["searchkey"] or start in r["_date"])]
        return {"announcements": rows, "hasMore": False}

    def get_response(self, url, retries=5):
        self.downloads.append(url)

        class Resp:
            content = b"%PDF fake"
            headers = {"Last-Modified": "Fri, 16 Oct 2026 08:00:19 GMT"}

        return Resp()


def row(aid, title, date_ms, date):
    return {"announcementId": aid, "announcementTitle": title, "announcementTime": date_ms, "adjunctUrl": f"finalpage/{date}/{aid}.PDF", "_date": date}


def test_provider_classifies_dates_and_replaces_the_express_report(tmp_path, monkeypatch):
    monkeypatch.setattr("valuation_monitor.providers.cninfo.excerpt_pages", lambda pdf: pdf)
    http = FakeHttp()
    ms = lambda s: int(pd.Timestamp(s, tz="Asia/Shanghai").timestamp() * 1000)  # noqa: E731
    http.lists[""] = [
        row("1", "2026年第三季度报告", ms("2026-10-17"), "2026-10-17"),
        row("2", "2026年第三季度报告（英文版）", ms("2026-10-17"), "2026-10-17"),
        row("3", "2026年第三季度报告摘要", ms("2026-10-17"), "2026-10-17"),
    ]
    http.lists["业绩快报"] = [row("4", "2025年度业绩快报", ms("2026-01-21"), "2026-01-21")]
    provider = CninfoProvider(http, tmp_path)
    found = provider.results_announcements("002415.SZ", datetime(2025, 12, 1))
    assert [(f.title, f.provisional) for f in found] == [("2025 annual express report", True), ("2026 Q3 report", False)]
    assert found[1].published == pd.Timestamp("2026-10-16 16:00:19")  # uses the PDF upload time, not the next-day 00:00 of the list
    index = json.loads((tmp_path / "002415.SZ" / "index.json").read_text(encoding="utf-8"))
    assert {e["id"] for e in index} == {"1", "4"}  # English versions and summaries are not downloaded

    n = len(http.downloads)
    provider.results_announcements("002415.SZ", datetime(2025, 12, 1))
    assert len(http.downloads) == n  # saved ones are not downloaded again

    # after the annual report is out: the express report is replaced, and the annual report keeps the express report's publication day
    http.lists[""].append(row("5", "2025年年度报告", ms("2026-04-18"), "2026-04-18"))
    found = provider.results_announcements("002415.SZ", datetime(2025, 12, 1))
    annual = next(f for f in found if f.title == "2025 annual report")
    assert not annual.provisional and annual.published == pd.Timestamp("2026-01-21 00:00")  # the annual report numbers first became public on the express report day
    assert "publication time kept from the express report" in annual.note and "2026-04-18" in annual.note  # the note says the numbers are the annual report's and the publication time is the express report's
    assert [f.title for f in found] == ["2025 annual report", "2026 Q3 report"]


@saved
def test_saved_reports_parse_and_match_the_key_table():
    index = json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))
    assert len(index) >= 50
    for e in index:
        content = (FOLDER / e["file"]).read_bytes()
        p = parse_cn_express(e["title"], content) if e["kind"] == "express" else parse_cn_report(e["title"], content)
        assert p.period == pd.Period(year=e["year"], quarter=e["quarter"], freq="Q")
        assert {"revenue", "profit", "eps", "equity", "wa_shares"} <= p.values.keys()


def parsed(title_prefix, kind="report"):
    e = next(e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if e["kind"] == kind and e["title"].startswith(title_prefix))
    content = (FOLDER / e["file"]).read_bytes()
    return (parse_cn_express if kind == "express" else parse_cn_report)(e["title"], content)


@saved
def test_third_quarter_uses_the_single_quarter_column():
    p = parsed("2026 Q3 report")
    assert p.quarterly and p.period == pd.Period("2026Q3")
    assert p.values["revenue"] == pytest.approx([27221.79484181]) and p.values["profit"] == pytest.approx([5015.68079576])
    assert p.values["eps"] == [0.547]  # single quarter, not the 1.409 cumulative for the first three quarters
    assert p.values["wa_shares"] == pytest.approx([9164.87155])  # period-end total shares, millions
    assert p.values["equity"] == pytest.approx([84274.81359135, 83348.18588562])


@saved
def test_annual_and_half_year_reports_are_cumulative_with_prior_year_columns():
    p = parsed("2025 annual report")
    assert not p.quarterly and p.period == pd.Period("2025Q4")
    assert p.values["eps"] == [1.546, 1.297] and p.values["profit"][0] == pytest.approx(14195.0, abs=1)
    assert parsed("2026 H1 report").values["eps"] == [0.862, 0.615]


@saved
def test_express_report_is_provisional_cumulative_data():
    p = parsed("2025 annual express report", "express")
    assert not p.quarterly and p.period == pd.Period("2025Q4")
    assert p.values["eps"] == [1.545, 1.297] and p.values["wa_shares"] == pytest.approx([9164.87155])


# --- Glodon (002410.SZ): a software company with a small finance business, so its statements differ from Hikvision's ---------

GLODON = Path(__file__).parent.parent / "data" / "filings" / "002410.SZ"
saved_glodon = pytest.mark.skipif(not (GLODON / "index.json").exists(), reason="no saved Glodon filings (the scheduled update saves them)")

GLODON_KEY_TABLE = (
    "一、主要财务数据 （一）主要会计数据和财务指标 项目 2025 年 2024 年 本年比上年增减 "
    "营业收入（元） 6,068,493,454.08 6,202,873,989.82 -2.17% "
    "归属于上市公司股东的净利润（元） 405,114,040.23 250,424,298.94 61.77% "
    "基本每股收益（元/股） 0.2467 0.1513 63.05% "
)
GLODON_INCOME = (
    "3、合并利润表 单位：元 项目 2025 年度 2024 年度 一、营业总收入 6,097,323,585.28 6,239,623,546.78 "
    "广联达科技股份有限公司 2025 年年度报告全文 70 "  # a page footer between the total and the operating revenue
    "其中：营业收入 6,068,493,454.08 6,202,873,989.82 利息收入 14,704,130.81 19,570,747.21 "
    "二、营业总成本 5,478,670,675.03 5,865,063,390.61 "
    "（二）按所有权归属分类 1.归属于母公司股东的净利润（净亏损以“-”号填列） 405,114,040.23 250,424,298.94 "
    "2.少数股东损益（净亏损以“-”号填列） 36,153,927.08 20,185,361.01 "
    "八、每股收益： （一）基本每股收益 0.2467 0.1513 （二）稀释每股收益 0.2461 0.1513 "
)
GLODON_BALANCE = "股本 1,651,944,342.00 1,651,944,342.00 资本公积 归属于母公司所有者权益合计 6,048,181,425.70 5,959,199,717.88 "


def test_operating_revenue_is_used_when_the_total_includes_interest_income(monkeypatch):
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: GLODON_KEY_TABLE + GLODON_INCOME + GLODON_BALANCE)
    p = parse_cn_report("2025 annual report", b"")
    assert p.values["revenue"] == pytest.approx([6068.49345408, 6202.87398982])  # not the total operating revenue 6,097.3
    assert p.values["profit"] == pytest.approx([405.11404023, 250.42429894])  # the item name carries the sign convention note
    assert p.values["eps"] == [0.2467, 0.1513]
    assert p.values["wa_shares"] == pytest.approx([1651.944342])


def test_express_report_with_eps_in_yuan_per_share(monkeypatch):
    text = (
        "项目 本报告期 上年同期 增减变动幅度（％） 营业总收入 6,586,834,560.79 5,619,380,098.61 17.22% "
        "归属于上市公司股东的净利润 962,811,015.59 660,974,286.88 45.67% "
        "扣除非经常性损益后的归属于上市公司股东的净利润 913,442,895.97 651,128,119.78 40.29% "
        "基本每股收益（元/股） 0.8179 0.5636 45.12% 加权平均净资产收益率 16.51% 10.59% "
        "项目 本报告期末 本报告期初 增减变动幅度（％） 总资产 11,547,911,676.63 10,067,626,141.36 14.70% "
        "归属于上市公司股东的所有者权益 6,308,736,999.42 5,787,737,027.21 9.00% 股本 1,191,298,268.00 1,189,373,056.00 0.16% "
    )
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    p = parse_cn_express("2022 annual express report", b"")
    assert p.values["eps"] == [0.8179, 0.5636]
    assert p.values["profit"] == pytest.approx([962.81101559, 660.97428688])  # not the figure excluding non-recurring items
    assert p.values["revenue"] == pytest.approx([6586.83456079, 5619.38009861])  # express reports give only the total operating revenue


def test_the_page_with_the_net_profit_is_kept_so_the_eps_page_after_it_is_too():
    from valuation_monitor.cn_patterns import is_wanted_page

    # Glodon's Q1 income statement runs over three pages: revenue, then profit, then EPS
    assert is_wanted_page("手续费及佣金收入 二、营业总成本 1.归属于母公司所有者的净利润 34,199,072.93 29,836,965.24")
    assert not is_wanted_page("非经常性损益项目和金额 非流动性资产处置损益 125,137.62")


@saved_glodon
def test_saved_glodon_reports_parse_and_match_the_key_table():
    index = json.loads((GLODON / "index.json").read_text(encoding="utf-8"))
    assert len(index) >= 60
    for e in index:
        content = (GLODON / e["file"]).read_bytes()
        p = parse_cn_express(e["title"], content) if e["kind"] == "express" else parse_cn_report(e["title"], content)
        assert p.period == pd.Period(year=e["year"], quarter=e["quarter"], freq="Q")
        profit, eps, shares = p.values["profit"][0], p.values["eps"][0], p.values["wa_shares"][0]
        assert profit / eps == pytest.approx(shares, rel=0.06), e["title"]  # period-end shares vs the weighted average behind the EPS


@saved_glodon
def test_saved_glodon_figures():
    def glodon(title):
        e = next(e for e in json.loads((GLODON / "index.json").read_text(encoding="utf-8")) if e["title"] == title)
        content = (GLODON / e["file"]).read_bytes()
        return (parse_cn_express if e["kind"] == "express" else parse_cn_report)(e["title"], content)

    annual = glodon("2025 annual report")
    assert annual.values["revenue"] == pytest.approx([6068.49345408, 6202.87398982]) and annual.values["eps"] == [0.2467, 0.1513]
    q1 = glodon("2026 Q1 report")  # the EPS is on the third page of the income statement
    assert q1.quarterly and q1.values["eps"] == [0.0209, 0.0181] and q1.values["revenue"][0] == pytest.approx(1155.89576812)
    q3 = glodon("2026 Q3 report")
    assert q3.values["eps"] == [0.045] and q3.values["wa_shares"] == pytest.approx([1651.944342])


# --- Innolight (300308.SZ, ChiNext): older report templates and a large balance sheet ---------------------------------------

INNOLIGHT = Path(__file__).parent.parent / "data" / "filings" / "300308.SZ"
saved_innolight = pytest.mark.skipif(not (INNOLIGHT / "index.json").exists(), reason="no saved Innolight filings (the scheduled update saves them)")


def test_thousands_separators_read_as_a_dot_and_spaces_are_repaired():
    assert clean_text("营业收入（元） 4.842 546 924 23 1,837,163,216.03 163.59%") == "营业收入（元） 4,842,546,924.23 1,837,163,216.03 163.59%"
    assert clean_text("基本每股收益（元/股） 0.731 0.512 42.77%") == "基本每股收益（元/股） 0.731 0.512 42.77%"  # untouched


def test_older_key_tables_label_the_total_and_the_ordinary_shareholders(monkeypatch):
    text = (
        "一、主要会计数据和财务指标 本报告期 上年同期 本报告期比上年同期增减 营业总收入（元） 1,398,598,185.75 14,564,576.12 9,502.74% "
        "归属于公司普通股股东的净利润（元） 147,766,656.23 -4,381,527.36 3,472.49% 基本每股收益（元/股） 0.31 -0.02 1,650.00% "
        "3、合并利润表 单位：元 项目 本期发生额 上期发生额 一、营业总收入 1,398,598,185.75 14,564,576.12 "
        "其中：营业收入 1,398,598,185.75 14,564,576.12 利息收入 已赚保费 二、营业总成本 1,243,721,773.73 20,480,646.88 "
        "归属于母公司所有者的净利润 147,766,656.23 -4,381,527.36 八、每股收益： （一）基本每股收益 0.31 -0.02 "
        "股本 474,661,948.00 474,661,948.00 资本公积 归属于母公司所有者权益合计 4,176,421,182.93 4,005,858,286.24 "
    )
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    p = parse_cn_report("2018 Q1 report", b"")
    assert p.values["revenue"] == pytest.approx([1398.59818575, 14.56457612]) and p.values["eps"] == [0.31, -0.02]
    assert p.values["profit"] == pytest.approx([147.76665623, -4.38152736])


def test_eps_rounded_to_two_decimals_in_the_income_statement_keeps_the_precise_figure(monkeypatch):
    text = (
        "主要会计数据和财务指标 2019 年 2018 年 本年比上年增减 营业收入（元） 4,757,567,128.02 5,156,511,018.44 -7.74% "
        "归属于上市公司股东的净利润（元） 516,616,327.08 668,094,659.05 -22.67% 基本每股收益（元/股） 0.731 0.951 -23.13% "
        "合并利润表 一、营业总收入 4,757,567,128.02 5,156,511,018.44 其中：营业收入 4,757,567,128.02 5,156,511,018.44 "
        "归属于母公司所有者的净利润 516,616,327.08 668,094,659.05 （一）基本每股收益 0.73 0.95 "
        "股本 713,031,166.00 475,354,111.00 资本公积 归属于母公司所有者权益合计 7,224,569,045.45 6,736,004,813.58 "
    )
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    assert parse_cn_report("2019 annual report", b"").values["eps"] == [0.731, 0.95]
    with pytest.raises(ValueError, match="eps"):  # a real difference still fails
        monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text.replace("（一）基本每股收益 0.73", "（一）基本每股收益 0.74"))
        parse_cn_report("2019 annual report", b"")


def test_a_balance_sheet_over_four_pages_is_kept_in_full_and_a_key_table_heading_keeps_the_next_page():
    from valuation_monitor.providers.cninfo import wanted_pages

    texts = [
        "目录",  # 0
        "四、主要会计数据和财务指标",  # 1: the heading ends the page...
        "营业收入（元） 43,076,710.47 58,185,324.15",  # 2: ...and the table starts on the next
        "管理层讨论与分析",  # 3
        "1、合并资产负债表 流动资产： 货币资金 53,532,493,243.56",  # 4
        "非流动资产： 固定资产",  # 5
        "流动负债： 短期借款",  # 6
        "所有者权益： 股本 1,177,909,641.00 1,111,118,334.00",  # 7
        "资本公积 归属于母公司所有者权益合计 1 2",  # 8
        "现金流量表",  # 9
        "附注",  # 10
    ]
    assert wanted_pages(texts) == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    # A company without subsidiaries has only a "balance sheet"; the parent-company balance sheet is not a start
    standalone = ["目录", "1、资产负债表 编制单位：山东中际电工装备股份有限公司", "所有者权益： 股本 216,010,800.00 216,010,800.00", "归属于母公司所有者权益合计 1 2", "附注", "附注", "附注"]
    assert wanted_pages(standalone) == [1, 2, 3, 4]
    assert wanted_pages(["目录", "2、母公司资产负债表 编制单位：某公司", "股本 1 2", "附注", "附注", "附注", "附注"]) == []


def test_share_capital_comes_from_the_balance_sheet_not_the_major_changes_table(monkeypatch):
    text = (
        "主要会计数据和财务指标 本报告期 上年同期 营业收入（元） 1,068,900,000.00 30.00% 归属于上市公司股东的净利润（元） 122,300,000.00 "
        "基本每股收益（元/股） 0.33 "
        "报告期主要财务数据、财务指标发生重大变动的情况及原因 项目 期初余额 期末余额 变动幅度 "
        "股本 216,010,800.00 473,857,056.00 119.37% 主要原因是本期发行股份购买资产 "
        "1、合并资产负债表 所有者权益： 股本 473,857,056.00 216,010,800.00 资本公积 3,523,845,291.06 232,337,575.52 "
        "归属于母公司所有者权益合计 4,225,900,000.00 555,782,210.10 "
        "合并年初到报告期末利润表 一、营业总收入 1,200,000,000.00 50,000,000.00 归属于母公司所有者的净利润 126,000,000.00 1,000,000.00 "
        "（一）基本每股收益 0.35 0.01 "
    )
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    p = parse_cn_report("2017 Q3 report", b"")
    assert p.values["wa_shares"] == pytest.approx([473.857056])  # period end, not the beginning figure of the changes table
    assert p.values["equity"] == pytest.approx([4225.9, 555.7822101])


def test_express_report_in_ten_thousand_yuan_and_shares(monkeypatch):
    text = (
        "一、2025 年度主要财务数据和指标 单位：人民币万元 项目 本报告期 上年同期 增减变动幅度 "
        "营业总收入 3,823,998.21 2,386,215.97 60.25% 归属于上市公司股东的净利润 1,079,863.45 517,148.60 108.81% "
        "基本每股收益（元） 9.80 4.72 107.63% 项目 本报告期末 本报告期初 增减变动幅度 "
        "归属于上市公司股东的所有者权益 3,000,398.71 1,913,388.70 56.81% 股本（万股） 111,111.83 112,116.65 -0.90% "
    )
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    p = parse_cn_express("2025 annual express report", b"")
    assert p.values["revenue"] == pytest.approx([38239.9821, 23862.1597])  # millions, not ten thousand times smaller
    assert p.values["profit"] == pytest.approx([10798.6345, 5171.486]) and p.values["eps"] == [9.8, 4.72]
    assert p.values["equity"] == pytest.approx([30003.9871, 19133.887]) and p.values["wa_shares"] == pytest.approx([1111.1183])


@saved_innolight
def test_saved_innolight_reports_parse():
    index = json.loads((INNOLIGHT / "index.json").read_text(encoding="utf-8"))
    assert len(index) >= 45
    for e in index:
        content = (INNOLIGHT / e["file"]).read_bytes()
        p = parse_cn_express(e["title"], content) if e["kind"] == "express" else parse_cn_report(e["title"], content)
        assert p.period == pd.Period(year=e["year"], quarter=e["quarter"], freq="Q")
        assert {"revenue", "profit", "eps", "equity", "wa_shares"} <= p.values.keys()


def cid_font_pdf(cids, ordering="GB1", to_unicode=None, cid_set=True):
    """A one-page PDF that shows `cids` in an Identity-H CID font of the Adobe `ordering` collection (no font program embedded)."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, StreamObject, TextStringObject

    def name(s):
        return NameObject(s)

    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    descriptor = DictionaryObject({name("/Type"): name("/FontDescriptor"), name("/FontName"): name("/TestFont"), name("/Flags"): NumberObject(4)})
    if cid_set:
        bits = bytearray((max(cids) // 8) + 1)
        for c in cids:
            bits[c // 8] |= 0x80 >> (c % 8)
        s = DecodedStreamObject()
        s.set_data(bytes(bits))
        descriptor[name("/CIDSet")] = writer._add_object(s)
    info = DictionaryObject({name("/Registry"): TextStringObject("Adobe"), name("/Ordering"): TextStringObject(ordering), name("/Supplement"): NumberObject(4)})
    descendant = DictionaryObject({name("/Type"): name("/Font"), name("/Subtype"): name("/CIDFontType0"), name("/BaseFont"): name("/TestFont"),
                                   name("/CIDSystemInfo"): info, name("/FontDescriptor"): writer._add_object(descriptor), name("/DW"): NumberObject(1000)})
    font = DictionaryObject({name("/Type"): name("/Font"), name("/Subtype"): name("/Type0"), name("/BaseFont"): name("/TestFont"),
                             name("/Encoding"): name("/Identity-H"), name("/DescendantFonts"): ArrayObject([writer._add_object(descendant)])})
    if to_unicode is not None:
        s = DecodedStreamObject()
        s.set_data(to_unicode.encode())
        font[name("/ToUnicode")] = writer._add_object(s)
    page[name("/Resources")] = DictionaryObject({name("/Font"): DictionaryObject({name("/F1"): writer._add_object(font)})})
    content = StreamObject()
    content._data = ("BT /F1 12 Tf 10 100 Td <" + "".join(f"{c:04X}" for c in cids) + "> Tj ET").encode()
    page[name("/Contents")] = writer._add_object(content)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_fonts_without_a_unicode_map_are_decoded_with_adobe_tables():
    from pypdf import PdfReader

    from valuation_monitor.cid_fonts import add_missing_unicode_maps, cid_to_unicode
    from valuation_monitor.cn_report import read_text

    gb1 = {chr(int(u, 16)): cid for cid, u in cid_to_unicode("GB1").items() if len(u) == 4}
    cids = [gb1[c] for c in "营业收入"]
    assert cids[1:] == [4156, 3436, 3270]  # what pypdf alone shows as the characters U+103C, U+0D6C, U+0CC6
    pdf = cid_font_pdf(cids)
    assert "营业收入" not in (PdfReader(io.BytesIO(pdf)).pages[0].extract_text() or "")
    assert read_text(pdf) == "营业收入"
    reader = PdfReader(io.BytesIO(pdf))
    assert add_missing_unicode_maps(reader) == 1
    unicode_map = reader.pages[0]["/Resources"]["/Font"]["/F1"].get_object()["/ToUnicode"].get_object().get_data().decode()
    assert unicode_map.count("beginbfchar") == 1 and "4 beginbfchar" in unicode_map  # only the CIDs in the font's CIDSet
    assert read_text(cid_font_pdf(cids, cid_set=False)) == "营业收入"  # without a CIDSet the whole table is used
    # traditional Chinese fonts (Adobe-CNS1) as well
    cns1 = {chr(int(u, 16)): cid for cid, u in cid_to_unicode("CNS1").items() if len(u) == 4}
    assert read_text(cid_font_pdf([cns1[c] for c in "營業收入"], ordering="CNS1")) == "營業收入"


def test_fonts_that_have_a_unicode_map_or_another_collection_are_left_alone():
    from pypdf import PdfReader

    from valuation_monitor.cid_fonts import add_missing_unicode_maps

    own_map = "begincmap 1 begincodespacerange <0000> <FFFF> endcodespacerange 1 beginbfchar <103C> <0041> endbfchar endcmap"
    pdf = cid_font_pdf([4156], to_unicode=own_map)
    reader = PdfReader(io.BytesIO(pdf))
    assert add_missing_unicode_maps(reader) == 0
    assert reader.pages[0].extract_text() == "A"  # the font's own map wins, even where Adobe's table says something else
    assert add_missing_unicode_maps(PdfReader(io.BytesIO(cid_font_pdf([4156], ordering="Japan1")))) == 0


def test_a_scanned_listing_is_skipped_for_a_same_day_listing_with_text(tmp_path, monkeypatch):
    monkeypatch.setattr("valuation_monitor.providers.cninfo.excerpt_pages", lambda pdf: pdf)
    monkeypatch.setattr("valuation_monitor.providers.cninfo.has_text", lambda pdf: pdf != b"scan")

    class Http(FakeHttp):
        def get_response(self, url, retries=5):
            resp = super().get_response(url, retries)
            resp.content = b"scan" if "/7." in url else b"text"
            return resp

    http = Http()
    ms = lambda s: int(pd.Timestamp(s, tz="Asia/Shanghai").timestamp() * 1000)  # noqa: E731
    http.lists[""] = [row("7", "2018年第一季度报告", ms("2018-04-26"), "2018-04-26"), row("8", "2018年第一季度报告", ms("2018-04-26"), "2018-04-26")]
    http.lists["业绩快报"] = []
    found = CninfoProvider(http, tmp_path).results_announcements("002415.SZ", datetime(2018, 1, 1))
    assert [f.url.rsplit("/", 1)[1] for f in found] == ["8.PDF"] and found[0].content == b"text"
    # with a single listing nothing is checked: the file is taken as before
    http, single = Http(), tmp_path / "single"
    http.lists[""], http.lists["业绩快报"] = [row("7", "2018年第一季度报告", ms("2018-04-26"), "2018-04-26")], []
    found = CninfoProvider(http, single).results_announcements("002415.SZ", datetime(2018, 1, 1))
    assert [f.url.rsplit("/", 1)[1] for f in found] == ["7.PDF"]


# --- China Northern Rare Earth (600111.SS): the first Shanghai-listed stock; its report templates differ from Shenzhen's --------

NORTHERN_RARE_EARTH = Path(__file__).parent.parent / "data" / "filings" / "600111.SS"
saved_northern = pytest.mark.skipif(
    not (NORTHERN_RARE_EARTH / "index.json").exists(), reason="no saved China Northern Rare Earth filings (the scheduled update saves them)"
)


def test_classify_shanghai_titles():
    assert classify("北方稀土2021年第三季度报告") == ("report", 2021, 3)  # the short name before the year
    assert classify("北方稀土2020年度报告全文") == ("report", 2020, 4)  # "annual report" right after the year, without a second "year" character
    assert classify("2019年度报告") == ("report", 2019, 4)
    assert classify("北方稀土2021年第一季度报告正文") == ("report", 2021, 1)
    assert classify("北方稀土2025年度<em>业绩</em><em>快报</em>公告") == ("express", 2025, 4)
    assert classify("北方稀土2022年半年度业绩快报公告") == ("express", 2022, 2)
    for skipped in ("北方稀土2025年年度报告摘要", "北方稀土2025年度业绩预告", "关于2025年年度报告的更正公告", "北方稀土2025年度社会责任报告"):
        assert classify(skipped) is None


# Text fragments of the 2025 annual report: the unit is in the table header, not in the rows, and "yuan/share" has a full-width slash
NORTHERN_ANNUAL = (
    "主要会计数据和财务指标 (一) 主要会计数据单位：元币种：人民币主要会计数据 2025年 2024年本期比上年同期增减(%) 2023年"
    "营业收入 42,563,083,233.34 32,966,314,898.03 29.11 33,496,993,130.09 利润总额 3,384,289,543.79 1,837,244,280.03 84.20 3,132,100,036.33 "
    "归属于上市公司股东的净利润 2,251,272,073.68 1,004,276,921.89 124.17 2,370,740,184.54 "
    "(二) 主要财务指标主要财务指标 2025年 2024年本期比上年同期增减(%) 2023年基本每股收益（元／股） 0.6227 0.2778 124.17 0.6558 "
    "合并资产负债表 所有者权益（或股东权益）： 实收资本（或股本） 七、53 3,615,065,842.00 3,615,065,842.00 "
    "归属于母公司所有者权益（或股东权益）合计 24,660,969,712.52 22,431,731,792.87 少数股东权益 "
    "合并利润表 一、营业总收入七、61 42,563,083,233.34 32,966,314,898.03 其中：营业收入七、61 42,563,083,233.34 32,966,314,898.03 "
    "二、营业总成本 39,273,756,306.11 31,311,132,511.63 "
    "1.归属于母公司股东的净利润（净亏损以“-”号填列） 2,251,272,073.68 1,004,276,921.89 2.少数股东损益 "
    "八、每股收益： （一）基本每股收益(元/股) 0.6227 0.2778 （二）稀释每股收益(元/股) 0.6227 0.2778 "
)


def test_shanghai_annual_report(monkeypatch):
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: NORTHERN_ANNUAL)
    p = parse_cn_report("2025 annual report", b"")
    assert not p.quarterly and p.values["eps"] == [0.6227, 0.2778]
    assert p.values["revenue"] == pytest.approx([42563.08323334, 32966.31489803])
    assert p.values["profit"] == pytest.approx([2251.27207368, 1004.27692189])
    assert p.values["equity"] == pytest.approx([24660.96971252, 22431.73179287])
    assert p.values["wa_shares"] == pytest.approx([3615.065842])


def test_notes_written_with_a_prefix_are_skipped(monkeypatch):
    text = NORTHERN_ANNUAL.replace("一、营业总收入七、61", "一、营业总收入附注七、61").replace("其中：营业收入七、61", "其中：营业收入附注七、61")
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    assert parse_cn_report("2025 annual report", b"").values["revenue"][0] == pytest.approx(42563.08323334)
    text = NORTHERN_ANNUAL.replace("一、营业总收入七、61", "一、营业总收入注释 40").replace("其中：营业收入七、61", "其中：营业收入")
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    assert parse_cn_report("2025 annual report", b"").values["revenue"][0] == pytest.approx(42563.08323334)


def test_a_page_break_inside_the_sign_convention_note(monkeypatch):
    # 2026 Q3: the last words of the net profit's sign convention note come after the numbers and the page footer
    text = NORTHERN_ANNUAL.replace(
        "（净亏损以“-”号填列） 2,251,272,073.68 1,004,276,921.89",
        "（净亏损以“-”号 2,251,272,073.68 1,004,276,921.89 中国北方稀土（集团）高科技股份有限公司 2025年年度报告 13 填列）",
    )
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    assert parse_cn_report("2025 annual report", b"").values["profit"] == pytest.approx([2251.27207368, 1004.27692189])


# Text fragments of the 2020 Q3 report: the key table has only year-to-date figures; the income statement has four columns
NORTHERN_OLD_Q3 = (
    "二、公司主要财务数据和股东变化 2.1 主要财务数据单位：元币种：人民币本报告期末上年度末本报告期末比上年度末增减(%) "
    "总资产 25,489,260,274.11 23,160,906,959.34 10.05 归属于上市公司股东的净资产 10,071,814,195.91 9,584,739,411.14 5.08 "
    "年初至报告期末（1-9 月）上年初至上年报告期末（1-9 月）比上年同期增减（%）营业收入 15,528,960,902.13 14,084,534,616.96 10.26 "
    "归属于上市公司股东的净利润 507,885,984.04 461,175,009.99 10.13 "
    "加权平均净资产收益率（%） 5.17 5.02 增加 0.15 个百分点基本每股收益（元/股） 0.1405 0.1269 10.72 稀释每股收益（元/股） 0.1405 0.1269 10.72 "
    "合并资产负债表 归属于母公司所有者权益（或股东权益）合计 10,071,814,195.91 9,584,739,411.14 少数股东权益 "
    "所有者权益（或股东权益）： 实收资本（或股本） 3,633,066,000.00 3,633,066,000.00 其他权益工具 "
    "合并利润表 项目 2020 年第三季度（7-9 月） 2019 年第三季度（7-9 月） 2020 年前三季度（1-9 月） 2019 年前三季度（1-9 月）"
    "一、营业总收入 5,918,394,483.24 5,565,314,416.50 15,528,960,902.13 14,084,534,616.96 "
    "其中：营业收入 5,918,394,483.24 5,565,314,416.50 15,528,960,902.13 14,084,534,616.96 二、营业总成本 "
    "（二）按所有权归属分类 1.归属于母公司股东的净利润（净亏损以“-”号填列） 179,722,749.23 195,144,932.44 507,885,984.04 461,175,009.99 "
    "八、每股收益： （一）基本每股收益(元/股) 0.1405 0.1269 （二）稀释每股收益(元/股) 0.1405 0.1269 "
)


def test_old_shanghai_q3_report_is_read_as_year_to_date(monkeypatch):
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: NORTHERN_OLD_Q3)
    p = parse_cn_report("2020 Q3 report", b"")
    assert not p.quarterly and p.period == pd.Period("2020Q3")  # year to date: Q3 = this - the half year
    assert p.values["revenue"] == pytest.approx([15528.96090213, 14084.53461696])
    assert p.values["profit"] == pytest.approx([507.88598404, 461.17500999])
    assert p.values["eps"] == [0.1405, 0.1269]
    assert p.values["equity"] == pytest.approx([10071.81419591, 9584.73941114]) and p.values["wa_shares"] == pytest.approx([3633.066])
    # 2015: the income statement also gives the Q3 EPS, so the year-to-date EPS are its third and fourth numbers
    text = NORTHERN_OLD_Q3.replace("（一）基本每股收益(元/股) 0.1405 0.1269", "（一）基本每股收益(元/股) 0.0495 0.0537 0.1405 0.1269")
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    assert parse_cn_report("2020 Q3 report", b"").values["eps"] == [0.1405, 0.1269]
    # the key table and the income statement must agree on the year to date
    text = NORTHERN_OLD_Q3.replace("0.1405 0.1269 （二）", "0.1505 0.1269 （二）")
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    with pytest.raises(ValueError, match="eps"):
        parse_cn_report("2020 Q3 report", b"")


def test_express_report_in_ten_thousand_yuan_with_share_capital_in_the_same_unit(monkeypatch):
    text = (
        "一、2024年度主要财务数据和指标单位：人民币万元项目本报告期上年同期增减变动幅度（%）营业总收入 3,296,631.49 3,349,699.31 -1.58 "
        "营业利润 184,009.52 311,419.58 -40.91 归属于上市公司股东的净利润 100,427.69 237,074.02 -57.64 "
        "基本每股收益（元） 0.2778 0.6558 -57.64 本报告期末本报告期初增减变动幅度（%）总资产 4,538,091.95 4,049,665.50 12.06 "
        "归属于上市公司股东的所有者权益 2,243,173.18 2,161,935.76 3.76 股本 361,506.58 361,506.58 0 "
    )
    monkeypatch.setattr("valuation_monitor.cn_report.read_text", lambda pdf: text)
    p = parse_cn_express("2024 annual express report", b"")
    assert p.values["revenue"] == pytest.approx([32966.3149, 33496.9931]) and p.values["eps"] == [0.2778, 0.6558]
    assert p.values["wa_shares"] == pytest.approx([3615.0658])  # millions of shares, not 0.36


def test_shanghai_key_data_and_balance_sheet_pages_are_kept():
    from valuation_monitor.providers.cninfo import wanted_pages

    texts = [
        "目录",  # 0
        "2.1 主要财务数据单位：元币种：人民币",  # 1: the key table of a quarterly report before 2021
        "股东总数",  # 2
        "附注",  # 3
        "合并资产负债表 流动资产：",  # 4
        "非流动资产：",  # 5
        "归属于母公司所有者权益（或股东权益）合计 1 2",  # 6: the equity total ends the balance sheet
        "母公司资产负债表",  # 7
        "附注", "附注", "附注",
    ]
    assert wanted_pages(texts) == [1, 2, 4, 5, 6, 7]


@saved_northern
def test_saved_northern_rare_earth_reports_parse():
    index = json.loads((NORTHERN_RARE_EARTH / "index.json").read_text(encoding="utf-8"))
    assert len(index) >= 50
    for e in index:
        content = (NORTHERN_RARE_EARTH / e["file"]).read_bytes()
        p = parse_cn_express(e["title"], content) if e["kind"] == "express" else parse_cn_report(e["title"], content)
        assert p.period == pd.Period(year=e["year"], quarter=e["quarter"], freq="Q")
        profit, eps, shares = p.values["profit"][0], p.values["eps"][0], p.values["wa_shares"][0]
        if abs(eps) >= 0.03:  # smaller EPS (2016 and 2017 Q1: 0.002, 0.02) are dominated by rounding
            assert profit / eps == pytest.approx(shares, rel=0.03), e["title"]


@saved_northern
def test_saved_northern_rare_earth_figures():
    def report(title):
        e = next(e for e in json.loads((NORTHERN_RARE_EARTH / "index.json").read_text(encoding="utf-8")) if e["title"] == title)
        content = (NORTHERN_RARE_EARTH / e["file"]).read_bytes()
        return (parse_cn_express if e["kind"] == "express" else parse_cn_report)(e["title"], content)

    q3, h1 = report("2020 Q3 report"), report("2020 H1 report")
    assert not q3.quarterly and q3.values["eps"] == [0.1405, 0.1269]
    # year to date - half year = the Q3 column of the income statement (5,918,394,483.24 yuan)
    assert q3.values["revenue"][0] - h1.values["revenue"][0] == pytest.approx(5918.39448324)
    assert report("2015 Q3 report").values["eps"] == [0.083, 0.109]  # year to date, not the 0.011 of the quarter
    new_q3 = report("2026 Q3 report")
    assert new_q3.quarterly and new_q3.values["eps"] == [0.4407] and new_q3.values["profit"] == pytest.approx([1592.92996878])
    assert report("2014 annual report").values["wa_shares"] == pytest.approx([2422.044])  # before the 10-for-5 conversion of 2015
    assert report("2025 annual express report").values["wa_shares"] == pytest.approx([3615.0658])
