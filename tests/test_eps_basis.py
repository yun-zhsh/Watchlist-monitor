"""The EPS basis of each PE metric (basic unless the config says diluted) and how it names the metric everywhere it is shown."""

import dataclasses

import pytest

from valuation_monitor.config import parse_config
from valuation_monitor.metrics import METRICS
from valuation_monitor.web import create_app


def stock(**extra):
    return parse_config({"stocks": [{"symbol": "BABA", "name": "Alibaba Group (ADR)", "cik": 1577552, **extra}]}).stocks[0]


def test_every_pe_is_basic_unless_configured():
    s = stock(metrics=["pe", "pe_non_gaap", "ps", "pb"])
    assert s.eps_basis == {}
    assert [s.metric_label(METRICS[k]) for k in s.metrics] == ["Basic PE (TTM)", "Basic PE Non-GAAP (TTM)", "PS (TTM)", "PB"]
    assert s.basis(METRICS["pe"]) == "basic" and s.basis(METRICS["ps"]) is None


def test_a_diluted_non_gaap_pe():
    s = stock(metrics=["pe", "pe_non_gaap", "ps", "pb"], eps_basis={"pe_non_gaap": "diluted"})
    assert s.metric_label(METRICS["pe"]) == "Basic PE (TTM)"
    assert s.metric_label(METRICS["pe_non_gaap"]) == "Diluted PE Non-GAAP (TTM)"
    assert s.basis(METRICS["pe_non_gaap"]) == "diluted"
    assert stock(metrics=["pe"], eps_basis={"pe": "basic"}).eps_basis == {}  # the default is not stored


@pytest.mark.parametrize(
    "metrics, basis, message",
    [
        (["pe", "pb"], {"pb": "diluted"}, "for the PE metrics"),  # PB is not based on EPS
        (["pe"], {"pe_non_gaap": "diluted"}, "for the PE metrics"),  # not a metric of the stock
        (["pe"], {"pe": "adjusted"}, "must be one of"),
    ],
)
def test_bad_eps_basis_is_refused(metrics, basis, message):
    with pytest.raises(ValueError, match=message):
        stock(metrics=metrics, eps_basis=basis)


def test_the_real_config_names_alibabas_non_gaap_pe_diluted_and_every_other_pe_basic():
    from pathlib import Path

    from valuation_monitor.config import load_config

    config = load_config(Path(__file__).parent.parent / "config.yaml")
    labels = {(s.symbol, k): s.metric_label(METRICS[k]) for s in config.stocks for k in s.metrics if METRICS[k].eps_based}
    assert labels[("BABA", "pe_non_gaap")] == "Diluted PE Non-GAAP (TTM)"
    assert labels[("BABA", "pe")] == "Basic PE (TTM)"
    assert labels[("JD", "pe_non_gaap")] == "Basic PE Non-GAAP (TTM)"
    assert labels[("0700.HK", "pe_non_ifrs")] == "Basic PE Non-IFRS (TTM)"
    assert all(label.startswith("Basic ") for key, label in labels.items() if key != ("BABA", "pe_non_gaap"))


# ---- the web pages and downloads -------------------------------------------------------------------------------------------


def client_for(monitor):
    app = create_app(monitor)
    app.testing = True
    return app.test_client()


def test_a_basic_stock_says_basic_on_the_pages_and_downloads(client):
    for path in ("/", "/stock/0700.HK/"):
        html = client.get(path).get_data(as_text=True)
        assert "Basic PE (TTM)" in html and "Basic PE Non-IFRS (TTM)" in html
        assert "Diluted" not in html
    eps = client.get("/stock/0700.HK/download/eps.csv").data.decode("utf-8-sig").splitlines()[0]
    assert "Basic EPS (Non-IFRS/Non-GAAP" in eps and "Basic EPS (IFRS/GAAP" in eps
    ttm = client.get("/stock/0700.HK/download/ttm.csv").data.decode("utf-8-sig").splitlines()[0]
    assert "TTM Non-IFRS/Non-GAAP basic EPS = sum of the periods" in ttm and "diluted" not in ttm.lower()
    daily = client.get("/stock/0700.HK/download/valuation.csv").data.decode("utf-8-sig").splitlines()[0]
    assert "Basic PE Non-IFRS (TTM) | Ratio = close / per-share value (price currency)" in daily


def test_a_diluted_non_gaap_pe_says_diluted_on_the_pages_and_downloads(monitor):
    """Like Alibaba: the Non-GAAP (here Non-IFRS) EPS is diluted, the IFRS/GAAP EPS stays basic."""
    config = parse_config(
        {"stocks": [{"symbol": "0700.HK", "name": "Tencent Holdings", "metrics": ["pe", "pe_non_ifrs"], "eps_basis": {"pe_non_ifrs": "diluted"}}]}
    )
    data = monitor.loader.load(None)
    monitor.config = config
    monitor.loader.load = lambda stock: dataclasses.replace(data, stock=config.stocks[0])
    client = client_for(monitor)

    index = client.get("/").get_data(as_text=True)
    assert "Basic PE (TTM)" in index and "Diluted PE Non-IFRS (TTM)" in index and "Basic PE Non-IFRS" not in index
    page = client.get("/stock/0700.HK/").get_data(as_text=True)
    assert "Diluted PE Non-IFRS (TTM)" in page and "Basic PE (TTM)" in page
    assert "Diluted PE Non-IFRS (TTM) is based on diluted EPS, because the company discloses no basic figure." in page
    eps = client.get("/stock/0700.HK/download/eps.csv").data.decode("utf-8-sig").splitlines()[0]
    assert "Diluted EPS (Non-IFRS/Non-GAAP" in eps and "Basic EPS (IFRS/GAAP" in eps
    ttm = client.get("/stock/0700.HK/download/ttm.csv").data.decode("utf-8-sig").splitlines()[0]
    assert "TTM Non-IFRS/Non-GAAP diluted EPS = sum of the periods" in ttm and "TTM EPS = sum of the periods' basic EPS" in ttm
    daily = client.get("/stock/0700.HK/download/valuation.csv").data.decode("utf-8-sig").splitlines()[0]
    assert "Diluted PE Non-IFRS (TTM) | Ratio" in daily and "Basic PE (TTM) | Ratio" in daily
