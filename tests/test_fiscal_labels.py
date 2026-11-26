"""Fiscal quarter labels: a company whose fiscal year does not end in December is shown as FY2025Q1 (the fiscal year named after
the calendar year it ends in), on the pages, in the downloads and in the MCP results. Inside the program the periods stay calendar
quarters; only the labels change (filings.period_label)."""

import io
import json
import re
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.config import load_config, parse_config
from valuation_monitor.data import StockData
from valuation_monitor.db import Database
from valuation_monitor.filings import Announcement, build_history, period_label
from valuation_monitor.mcp_server import ValuationService
from valuation_monitor.service import Monitor
from valuation_monitor.web import create_app

ROOT = Path(__file__).resolve().parent.parent
Q = lambda text: pd.Period(text, freq="Q")  # noqa: E731


@pytest.mark.parametrize(
    "fiscal_year_end,calendar_quarter,label",
    [
        # December year end: the labels do not change
        (12, "2025Q1", "2025Q1"),
        (12, "2025Q4", "2025Q4"),
        # March (Alibaba, Dynatrace): FY2025 runs from April 2024 to March 2025
        (3, "2024Q2", "FY2025Q1"),
        (3, "2024Q3", "FY2025Q2"),
        (3, "2024Q4", "FY2025Q3"),
        (3, "2025Q1", "FY2025Q4"),
        (3, "2026Q3", "FY2027Q2"),
        # June (Microsoft): FY2025 runs from July 2024 to June 2025
        (6, "2024Q3", "FY2025Q1"),
        (6, "2024Q4", "FY2025Q2"),
        (6, "2025Q1", "FY2025Q3"),
        (6, "2025Q2", "FY2025Q4"),
        # September (Apple): FY2025 runs from October 2024 to September 2025; its fourth quarter ends late September
        (9, "2024Q4", "FY2025Q1"),
        (9, "2025Q2", "FY2025Q3"),
        (9, "2025Q3", "FY2025Q4"),
        # January (NVIDIA): FY2025 runs from February 2024 to January 2025; its quarters are kept under the calendar quarter of
        # their end minus 45 days (FY2025Q1 ended 2024-04-28: 2024Q1)
        (1, "2024Q1", "FY2025Q1"),
        (1, "2024Q2", "FY2025Q2"),
        (1, "2024Q3", "FY2025Q3"),
        (1, "2024Q4", "FY2025Q4"),
    ],
)
def test_quarter_labels(fiscal_year_end, calendar_quarter, label):
    assert period_label(Q(calendar_quarter), 4, fiscal_year_end) == label


@pytest.mark.parametrize(
    "end,label",
    [("2024-04-28", "FY2025Q1"), ("2024-07-28", "FY2025Q2"), ("2024-10-27", "FY2025Q3"), ("2025-01-26", "FY2025Q4"), ("2025-04-27", "FY2026Q1")],
)
def test_nvidia_quarters_by_their_end_dates(end, label):
    """NVIDIA's fiscal quarters end about four weeks after a calendar quarter; its branch keys them by their end minus 45 days."""
    assert period_label(pd.Period(pd.Timestamp(end) - pd.Timedelta(days=45), freq="Q"), 4, 1) == label


@pytest.mark.parametrize("fiscal_year_end", range(1, 12))
@pytest.mark.parametrize("days_after_month_end", [-7, 0, 3])
def test_every_fiscal_year_end_labels_its_own_quarters(fiscal_year_end, days_after_month_end):
    """For every year-end month: each fiscal quarter (ending on the last day of its month, or up to a week before or a few days
    after, as with 52/53-week years), kept under the calendar quarter of its end minus 45 days, gets its own label."""
    for year in (2024, 2025):
        for quarter in range(1, 5):
            months_after_year_end = 3 * quarter - 12
            end_month = pd.Period(year=year, month=fiscal_year_end, freq="M") + months_after_year_end
            end = end_month.end_time.normalize() + pd.Timedelta(days=days_after_month_end)
            internal = pd.Period(end - pd.Timedelta(days=45), freq="Q")
            assert period_label(internal, 4, fiscal_year_end) == f"FY{year}Q{quarter}", (fiscal_year_end, end)


def test_half_year_and_annual_labels():
    assert period_label(Q("2025Q2"), 2) == "2025H1" and period_label(Q("2025Q4"), 2) == "2025H2" and period_label(Q("2025Q4"), 1) == "2025FY"
    assert period_label(Q("2024Q3"), 2, 3) == "FY2025H1" and period_label(Q("2025Q1"), 2, 3) == "FY2025H2"
    assert period_label(Q("2025Q1"), 1, 3) == "FY2025" and period_label(Q("2025Q2"), 1, 6) == "FY2025"


# config.yaml

def test_fiscal_year_end_is_a_month():
    stock = {"symbol": "BABA", "name": "Alibaba Group (ADR)", "cik": 1577552}
    assert parse_config({"stocks": [stock]}).stocks[0].fiscal_year_end == 12
    assert parse_config({"stocks": [{**stock, "fiscal_year_end": 3}]}).stocks[0].fiscal_year_end == 3
    for bad in (0, 13, "3", 3.0, True, None):
        with pytest.raises(ValueError, match="fiscal_year_end"):
            parse_config({"stocks": [{**stock, "fiscal_year_end": bad}]})


def test_alibaba_and_dynatrace_end_their_fiscal_year_in_march():
    ends = {s.symbol: s.fiscal_year_end for s in load_config(ROOT / "config.yaml").stocks}
    assert ends["BABA"] == 3 and ends["DT"] == 3
    assert ends["MSFT"] == 6  # Microsoft's fiscal year ends on June 30
    assert ends["AAPL"] == 9  # Apple's fiscal year ends on the last Saturday of September
    assert all(end == 12 for symbol, end in ends.items() if symbol not in {"BABA", "DT", "MSFT", "AAPL"})


# what is shown: the pages, the downloads and the MCP results of a March year end

def announcement(period, published, eps, revenue, profit, equity):
    return Announcement(Q(period), pd.Timestamp(published), f"https://filings.example/{Q(period).end_time:%Y-%m-%d}.htm", "CNY", True,
                        {"eps": eps, "revenue": revenue, "profit": profit, "equity": equity})


ANNOUNCEMENTS = [
    announcement("2024Q2", "2024-08-15 17:00", [2.0, 1.5, 1.0], [110.0, 95.0, 100.0], [20.0, 15.0, 10.0], [520.0, 480.0]),
    announcement("2024Q3", "2024-11-15 17:00", [3.0, 2.5, 2.0], [120.0, 100.0, 110.0], [30.0, 25.0, 20.0], [540.0, 480.0]),
    announcement("2024Q4", "2025-02-20 17:00", [4.0, 3.5, 3.0], [130.0, 105.0, 120.0], [40.0, 35.0, 30.0], [560.0, 480.0]),
    announcement("2025Q1", "2025-05-15 17:00", [5.0, 4.0, 4.0], [140.0, 110.0, 130.0], [50.0, 40.0, 40.0], [580.0, 560.0]),
    announcement("2025Q2", "2025-08-29 17:00", [6.0, 5.0, 5.0], [150.0, 115.0, 140.0], [60.0, 50.0, 50.0], [600.0, 560.0]),
]
DAYS = pd.bdate_range("2024-01-01", "2025-12-31")
CLOSE = pd.Series([100.0 + (i * 7) % 40 for i in range(len(DAYS))], index=DAYS)
CALENDAR = re.compile(r"\b20\d\dQ[1-4]\b")  # a calendar quarter label, which a March year end must never show


def fiscal_config():
    return parse_config({"stocks": [{"symbol": "BABA", "name": "Alibaba Group (ADR)", "cik": 1577552, "metrics": ["pe", "ps", "pb"],
                                     "fiscal_year_end": 3}], "lookback_options": [1]})


def fiscal_monitor(tmp_path, readonly=False):
    config = fiscal_config()
    writer = Database(tmp_path / "valuation.db")
    final = pd.DataFrame({"close": CLOSE, "source": "yahoo", "status": "yahoo_only", "tencent_close": None, "yahoo_close_unadjusted": CLOSE,
                          "yahoo_factor": 1.0}, index=CLOSE.index.rename("date"))
    writer.write_series("prices_final", {"symbol": "BABA"}, final)
    earnings = build_history(list(ANNOUNCEMENTS), fiscal_year_end=config.stocks[0].fiscal_year_end)
    writer.write_frame("periods", "BABA", earnings.periods_frame())
    writer.write_frame("ttm", "BABA", earnings.ttm_frame(), date_format="%Y-%m-%d")
    monitor = Monitor(config, tmp_path, tmp_path / "cache", offline=True, readonly=readonly)
    data = StockData(config.stocks[0], CLOSE, "USD", pd.Series(0.14, index=DAYS), earnings.ttm, earnings)
    monitor.loader.load = lambda stock: data
    monitor.price_check = lambda symbol: {}
    return monitor


def test_the_history_carries_fiscal_labels_only():
    earnings = build_history(list(ANNOUNCEMENTS), fiscal_year_end=3)
    # still calendar quarters inside (the filings also state the year before)
    assert sorted(earnings.quarters["eps"]) == list(pd.period_range("2023Q2", "2025Q2", freq="Q"))
    assert list(earnings.periods_frame()["period"]) == [f"FY{y}Q{q}" for y in (2024, 2025) for q in range(1, 5)] + ["FY2026Q1"]
    last = earnings.ttm_detail[-1]
    assert last["period_label"] == "FY2026Q1" and last["periods"] == ["FY2025Q2", "FY2025Q3", "FY2025Q4", "FY2026Q1"]
    ttm = earnings.ttm_frame()
    assert list(ttm.iloc[-1][["period_1", "period_2", "period_3", "period_4"]]) == ["FY2025Q2", "FY2025Q3", "FY2025Q4", "FY2026Q1"]
    assert ttm.iloc[-1]["eps_ttm_formula"].startswith("FY2025Q2 3")
    # the numbers are the same as with calendar labels
    calendar = build_history(list(ANNOUNCEMENTS))
    assert earnings.ttm.equals(calendar.ttm)
    assert calendar.ttm_detail[-1]["period_label"] == "2025Q2"


def test_pages_and_downloads_show_fiscal_labels(tmp_path):
    app = create_app(fiscal_monitor(tmp_path))
    app.testing = True
    client = app.test_client()
    page = client.get("/stock/BABA/").get_data(as_text=True)
    charts = json.loads(re.search(r'<script type="application/json" id="view-data">(.*?)</script>', page, re.S).group(1))["charts"]
    titles = [r["title"] for m in charts["metrics"] for r in m["records"].values()]
    assert titles and all(t.startswith("FY") for t in titles) and any(t.startswith("FY2026Q1 results") for t in titles)
    lines = " ".join(line for m in charts["metrics"] for r in m["records"].values() for line in r["lines"])
    assert "FY2025Q2" in lines and not CALENDAR.search(lines)
    texts = {}
    for name in ("eps.csv", "ttm.csv", "valuation.csv"):
        r = client.get(f"/stock/BABA/download/{name}")
        assert r.status_code == 200, name
        texts[name] = r.data.decode("utf-8-sig")
    assert "FY2026Q1" in texts["eps.csv"] and "FY2025Q2" in texts["ttm.csv"] and "FY2026Q1" in texts["valuation.csv"]
    with zipfile.ZipFile(io.BytesIO(client.get("/stock/BABA/download/raw.zip").data)) as z:
        texts.update({n: z.read(n).decode("utf-8-sig", errors="replace") for n in z.namelist() if n.endswith(".csv")})
    for name, text in texts.items():
        assert not CALENDAR.search(text), (name, CALENDAR.search(text).group())


def test_mcp_results_show_fiscal_labels(tmp_path):
    service = ValuationService(fiscal_monitor(tmp_path, readonly=True), today=pd.Timestamp("2025-12-31").date())
    assert service.list_stocks()["stocks"][0]["fiscal_year_end_month"] == 3
    result = service.valuation("BABA", "pb")
    record = result["sources"]["earnings_record"]
    assert record["period"] == "FY2026Q1" and record["filing_url"] == "https://filings.example/2025-06-30.htm"  # still found by its period
    assert {c["period"] for c in result["sources"]["components"]} == {"FY2026Q1"}
    assert "FY2026Q1" in " ".join(result["calculation"]["per_share_value"])
    pe = service.valuation("BABA", "pe")
    assert [c["period"] for c in pe["sources"]["components"]] == ["FY2025Q2", "FY2025Q3", "FY2025Q4", "FY2026Q1"]
    assert all(c["filing_url"] for c in pe["sources"]["components"])
    # point in time: as of a day before the last filing, the record before it, with its fiscal label
    earlier = service.valuation("BABA", "pe", as_of="2025-08-01")
    assert earlier["sources"]["earnings_record"]["period"] == "FY2025Q4"
    filings = service.filings("BABA")["filings"]
    assert [f["period"] for f in filings] == ["FY2026Q1", "FY2025Q4", "FY2025Q3", "FY2025Q2", "FY2025Q1"]
    assert not CALENDAR.search(json.dumps([result, pe, earlier, filings]))


def test_the_saved_alibaba_and_dynatrace_data_get_fiscal_labels():
    """The real saved filings of the two March year ends: every label is fiscal, and the internal periods are unchanged."""
    from valuation_monitor.mcp_server import open_service

    service = open_service(str(ROOT / "config.yaml"), str(ROOT / "data"))
    for symbol in ("BABA", "DT"):
        filings = service.filings(symbol, limit=50)["filings"]
        if not filings:
            pytest.skip(f"no saved {symbol} data")
        assert all(re.fullmatch(r"FY20\d\dQ[1-4]", f["period"]) for f in filings), symbol
        result = service.valuation(symbol, "pe")
        assert not CALENDAR.search(json.dumps(result["sources"]) + " ".join(result["calculation"]["per_share_value"])), symbol
