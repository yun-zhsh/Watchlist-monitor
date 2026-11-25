import io
import zipfile

import pandas as pd
import pytest

from valuation_monitor.service import Monitor
from valuation_monitor.web import export_site


def test_footer_shows_the_version(client):
    from valuation_monitor import __version__

    assert f"Stock Valuation Monitor v{__version__}" in client.get("/").get_data(as_text=True)


def test_index_lists_stocks(client):
    html = client.get("/").get_data(as_text=True)
    assert "Tencent Holdings" in html and "0700.HK" in html
    assert "/stock/0700.HK/" in html
    assert "Update data" in html
    assert "Basic PE (TTM)" in html and "percentile" in html


def test_stock_page_has_charts_and_downloads(client):
    html = client.get("/stock/0700.HK/").get_data(as_text=True)
    assert 'id="chart-pe"' in html and 'id="chart-pe_non_ifrs"' in html and 'id="chart-price"' in html
    for name in ("prices.csv", "eps.csv", "ttm.csv", "raw.zip"):
        assert f"/stock/0700.HK/download/{name}" in html
    assert 'download="0700.HK_historical_prices.csv"' in html
    assert client.get("/stock/0700.HK").status_code == 308  # a URL without the trailing / redirects
    assert client.get("/stock/9999.HK/").status_code == 404


def test_downloads(client):
    r = client.get("/stock/0700.HK/download/prices.csv")
    assert r.status_code == 200
    text = r.data.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Date,Close (actual traded price)")
    assert "Tencent,Agree" in text
    assert "historical_prices" in r.headers["Content-Disposition"]

    assert client.get("/stock/0700.HK/download/eps.csv").data.decode("utf-8-sig").startswith("Period (quarter or half year),Currency")
    names = zipfile.ZipFile(io.BytesIO(client.get("/stock/0700.HK/download/raw.zip").data)).namelist()
    assert "prices/0700.HK/final.csv" in names and "eps/0700.HK_periods.csv" in names
    assert client.get("/stock/0700.HK/download/other").status_code == 404


def test_refresh_redirects_back(client):
    r = client.post("/refresh", data={"next": "/stock/0700.HK/"})
    assert r.status_code == 302 and r.headers["Location"].startswith("/stock/0700.HK/?updated=")
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Update finished at" in page and ("new data loaded" in page or "no new data" in page)
    for target in ("https://example.com", "//example.com"):
        r = client.post("/refresh", data={"next": target})
        assert "example.com" not in r.headers["Location"]


def test_export_site(monitor, tmp_path):
    out = tmp_path / "site"
    export_site(monitor, out, base_path="/Claude")
    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/Claude/stock/0700.HK/"' in index
    assert "/Claude/static/style.css" in index
    assert "/refresh" not in index and "Update data" not in index  # the static site has no update button
    page = (out / "stock" / "0700.HK" / "index.html").read_text(encoding="utf-8")
    assert "/Claude/stock/0700.HK/download/prices.csv" in page and "updated automatically on a schedule" in page
    assert (out / "stock" / "0700.HK" / "download" / "prices.csv").read_bytes().decode("utf-8-sig").startswith("Date")
    assert (out / "static" / "chart.umd.min.js").exists()
    assert (out / ".nojekyll").exists()


def test_drop_empty_series():
    from valuation_monitor.web.downloads import drop_empty_series

    df = pd.DataFrame(
        {
            "period": ["2025H1"],
            "eps": [1.5], "eps_published": ["2025-08-19 18:28"], "eps_effective": ["2025-08-20"], "eps_sources": [2], "eps_max_deviation": [0.0],
            "non_ifrs_eps": [None], "non_ifrs_eps_published": [None], "non_ifrs_eps_effective": [None],
            "non_ifrs_eps_sources": [0], "non_ifrs_eps_max_deviation": [None],
        }
    )
    out = drop_empty_series(df)
    assert list(out.columns) == ["period", "eps", "eps_published", "eps_effective", "eps_sources", "eps_max_deviation"]


def test_download_headers_are_unique():
    import collections

    from valuation_monitor.web import downloads

    for columns in (downloads.PERIOD_COLUMNS, downloads.TTM_COLUMNS, downloads.PRICE_COLUMNS):
        names = list(columns.values())
        assert [n for n, c in collections.Counter(names).items() if c > 1] == []


def view_json(html):
    import json
    import re

    return json.loads(re.search(r'<script type="application/json" id="view-data">(.*?)</script>', html, re.S).group(1))


def test_index_has_years_dropdown_and_all_windows(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="years-select"' in html
    for y in (10, 5, 3, 2, 1):
        assert f'<option value="{y}"' in html and f"Past {y} {'year' if y == 1 else 'years'}" in html
    assert '<option value="10" selected>' in html  # default 10 years
    assert 'data-keep-years' in html and 'data-ref="0700.HK|pe"' in html and "static/app.js" in html

    view = view_json(html)
    assert view["default"] == 10 and view["options"] == [10, 5, 3, 2, 1]
    windows = view["fields"]["0700.HK|pe"]
    assert set(windows) == {"10", "5", "3", "2", "1"}  # the order of keys does not matter; the page looks them up by key
    f = windows["1"]
    assert set(f) == {"current", "percentile", "level", "color", "range", "stats", "bands", "start", "band_values"}
    assert f["start"] > windows["10"]["start"]  # the one-year window starts later than the ten-year window
    assert set(f["band_values"]) == {"P10", "P30", "P50", "P70", "P90"}


def test_stock_page_has_chart_data_for_every_window(client):
    html = client.get("/stock/0700.HK/").get_data(as_text=True)
    view = view_json(html)
    assert view["symbol"] == "0700.HK"
    charts = view["charts"]
    assert {m["key"] for m in charts["metrics"]} == {"pe", "pe_non_ifrs"}
    assert set(charts["price"]["starts"]) == {"10", "5", "3", "2", "1"}
    # the chart data covers the longest lookback; the page cuts it to the selected years
    assert charts["metrics"][0]["dates"][0] >= view["fields"]["0700.HK|pe"]["10"]["start"]
    assert 'data-f="percentile"' in html and "ValuationView.onChange" in html
    assert 'href="/"' in html and "data-keep-years" in html


def test_export_includes_app_script(monitor, tmp_path):
    out = tmp_path / "site2"
    export_site(monitor, out, base_path="/Claude")
    assert (out / "static" / "app.js").exists()
    assert 'id="years-select"' in (out / "index.html").read_text(encoding="utf-8")


def test_valuation_download_shows_how_each_ratio_is_calculated(client):
    import io

    raw = client.get("/stock/0700.HK/download/valuation.csv").data.decode("utf-8-sig")
    df = pd.read_csv(io.StringIO(raw))
    close = "Close (price currency)"
    converted = "Basic PE (TTM) | Per-share value (price currency) = per-share value x FX rate"
    ratio = "Basic PE (TTM) | Ratio = close / per-share value (price currency)"
    ok = df[ratio].notna()
    assert ok.any()
    assert ((df.loc[ok, close] / df.loc[ok, converted]) - df.loc[ok, ratio]).abs().max() < 1e-9
    assert (df["Basic PE (TTM) | Per-share value (reporting currency)"] * df["FX rate (1 unit of reporting currency = ? price currency)"] - df[converted]).abs().max() < 1e-9
    assert df.loc[ok, "Basic PE (TTM) | Earnings record (period)"].iloc[-1] == "2024Q2"


def test_ttm_download_shows_the_components_and_formulas(client):
    import io

    df = pd.read_csv(io.StringIO(client.get("/stock/0700.HK/download/ttm.csv").data.decode("utf-8-sig")))
    row = df[df["TTM period 4"] == "2024Q2"].iloc[0]
    parts = [row[f"Period {k}: basic EPS"] for k in range(1, 5)]
    eps_ttm = "TTM EPS = sum of the periods' basic EPS"
    assert row[eps_ttm] == pytest.approx(sum(parts))
    assert row["TTM basic EPS formula (* = obtained by subtracting cumulative figures)"].endswith(f"= {row[eps_ttm]:g}")
    assert "2023Q3" in row["TTM basic EPS formula (* = obtained by subtracting cumulative figures)"]


def test_chart_payload_carries_the_calculation_for_the_tooltip(client):
    html = client.get("/stock/0700.HK/").get_data(as_text=True)
    charts = view_json(html)["charts"]["metrics"][0]
    n = len(charts["dates"])
    assert len(charts["price"]) == len(charts["rec"]) == n
    assert charts["fx"] and len(charts["fx"]) == n and charts["price_ccy"] == "HKD" and charts["report_ccy"] == "CNY"
    rec = charts["records"][str(charts["rec"][-1])]
    assert "TTM EPS" in rec["lines"][0] and "2024Q2" in rec["title"]
    # the ratio recomputed in the tooltip matches the value on the trend chart
    k = n - 1
    assert charts["price"][k] / (rec["per_share"] * charts["fx"][k]) == pytest.approx(charts["values"][k], abs=0.006)


# The home page order ("Order" panel, home_order.py)

def ordered_monitor(monitor, github_repository="yun-zhsh/Watchlist-monitor"):
    """The fixture's monitor with three stocks: JD hidden, in the order of the config."""
    from valuation_monitor.config import parse_config

    monitor.config = parse_config({
        "github_repository": github_repository,
        "stocks": [
            {"symbol": "JD", "name": "JD.com (ADR)", "market": "US", "cik": 1, "hidden": True},
            {"symbol": "0700.HK", "name": "Tencent Holdings"},
            {"symbol": "2382.HK", "name": "Sunny Optical Technology"},
        ],
    })
    columns = ["close", "source", "status", "tencent_close", "yahoo_close_unadjusted", "yahoo_factor"]
    final = monitor.db.read_series("prices_final", {"symbol": "0700.HK"}, columns)
    for symbol in ("JD", "2382.HK"):  # the saved data of the fixture, so that every stock has its downloads
        monitor.db.write_series("prices_final", {"symbol": symbol}, final)
        for table in ("periods", "ttm"):
            monitor.db.write_frame(table, symbol, monitor.db.read_frame(table, "0700.HK"), date_format="%Y-%m-%d" if table == "ttm" else None)
    return monitor


def card_symbols(html):
    import re

    return re.findall(r'data-symbol="([^"]+)" data-name="[^"]+"( hidden)?>', html)


def test_index_has_the_order_button_and_panel(client):
    html = client.get("/").data.decode()
    assert 'id="order-open"' in html and ">Order</button>" in html
    assert html.index('id="years-select"') < html.index('id="order-open"')  # next to the History drop-down
    assert '<dialog id="order-panel"' in html and ">Undo changes</button>" in html and ">Show all</button>" in html and "data-order-save" in html and "data-token-forget" in html
    assert '"Actions": "Read and write"' in html and "static/home-order.js" in html
    assert "Reset to default" not in html  # no fixed default order: Undo changes and Show all
    assert view_json(html)["home_order"] == {"url": None, "repository": None, "workflow": "home-order.yml", "ref": "main"}


def test_the_order_button_is_on_the_home_page_only(client):
    html = client.get("/stock/0700.HK/").data.decode()
    assert 'id="order-open"' not in html and "home-order.js" not in html and 'id="years-select"' in html


def test_index_follows_the_config_order_and_hides_hidden_stocks(monitor):
    from valuation_monitor.web import create_app

    client = create_app(ordered_monitor(monitor)).test_client()
    html = client.get("/").data.decode()
    assert card_symbols(html) == [("JD", " hidden"), ("0700.HK", ""), ("2382.HK", "")]  # hidden stays in the page for the panel
    assert view_json(html)["home_order"]["url"] == "https://yun-zhsh.github.io/Watchlist-monitor/home-order.json"
    assert view_json(html)["home_order"]["repository"] == "yun-zhsh/Watchlist-monitor"
    # hidden concerns the home page only: the stock page and the downloads still work
    assert client.get("/stock/JD/").status_code == 200
    assert client.get("/stock/JD/download/prices.csv").status_code == 200

    monitor.config = type(monitor.config)(**{**monitor.config.__dict__, "stocks": monitor.config.stocks[::-1]})
    client = create_app(monitor).test_client()
    assert [s for s, _ in card_symbols(client.get("/").data.decode())] == ["2382.HK", "0700.HK", "JD"]


def test_export_publishes_home_order_json(monitor, tmp_path):
    import json

    out = tmp_path / "site"
    export_site(ordered_monitor(monitor), out, base_path="/x", offline=True)
    assert json.loads((out / "home-order.json").read_text(encoding="utf-8")) == {"order": ["JD", "0700.HK", "2382.HK"], "hidden": ["JD"]}
    assert card_symbols((out / "index.html").read_text(encoding="utf-8"))[0] == ("JD", " hidden")
    assert (out / "static" / "home-order.js").exists()
    assert monitor.offline is False  # the offline export does not leave the monitor offline


def test_export_without_github_repository_still_publishes_the_order(monitor, tmp_path):
    out = tmp_path / "site"
    export_site(monitor, out)
    assert (out / "home-order.json").read_text(encoding="utf-8").startswith('{\n  "order": [\n    "0700.HK"')
