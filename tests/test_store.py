from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.db import PRICE_COLUMNS
from valuation_monitor.filings import Announcement
from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.yahoo import PriceHistory
from valuation_monitor.store import EarningsStore, PriceStore

from valuation_monitor import store as store_module

# One instant for the whole module: a test that runs across midnight in Hong Kong would otherwise see "today" move under it
NOW = pd.Timestamp.now(tz="UTC")
TODAY = NOW.tz_convert("Asia/Hong_Kong").tz_localize(None).normalize()


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(store_module, "local_today", lambda timezone: NOW.tz_convert(timezone).tz_localize(None).normalize())


def days_ago(n):
    return TODAY - pd.Timedelta(days=n)


def bars(closes, volume=100, extra=()):
    df = pd.DataFrame({"close": pd.Series(closes, dtype=float)}).sort_index()
    for col in ("open", "high", "low", *extra):
        df[col] = df["close"]
    df["volume"] = volume
    return df


class FakeQuotes:
    currency = "HKD"
    timezone = "Asia/Hong_Kong"

    def __init__(self, closes):
        self.closes = closes  # {date: closing price}
        self.calls = []
        self.offline = False

    def daily_bars(self, symbol, start):
        if self.offline:
            raise ConnectionError("offline")
        self.calls.append(pd.Timestamp(start))
        b = bars(self.closes)
        return b[b.index >= pd.Timestamp(start)]


class FakeYahoo:
    def __init__(self, closes, splits=None):
        self.closes = closes
        self.splits = splits or {}
        self.calls = []
        self.offline = False

    def price_history(self, symbol, start):
        if self.offline:
            raise ConnectionError("offline")
        self.calls.append(pd.Timestamp(start))
        b = bars(self.closes, extra=("adjclose",))
        b = b[b.index >= pd.Timestamp(start)]
        return PriceHistory(b["close"], pd.Series(self.splits, dtype=float), "HKD", "Asia/Hong_Kong", bars=b)


def saved(store, symbol, source):
    """The raw bars of one source saved in the database."""
    return store.db.read_series("price_bars", {"symbol": symbol, "source": source}, PRICE_COLUMNS[source])


def test_price_store_saves_each_source_and_fetches_incrementally(tmp_path):
    quotes = FakeQuotes({days_ago(30): 100.0, days_ago(2): 101.0, days_ago(1): 102.0, TODAY: 103.0})
    yahoo = FakeYahoo({days_ago(30): 95.0, days_ago(2): 95.95, days_ago(1): 96.9, TODAY: 97.85})  # factor 0.95
    store = PriceStore(quotes, yahoo, tmp_path)

    h = store.prices("0700.HK", days_ago(40).to_pydatetime())
    assert h.close.tolist() == [100.0, 101.0, 102.0, 103.0]  # includes today's live price
    assert saved(store, "0700.HK", "tencent")["close"].tolist() == [100.0, 101.0, 102.0]  # today has not closed, not saved
    assert saved(store, "0700.HK", "yahoo")["close"].tolist() == [95.0, 95.95, 96.9]
    final = store.final("0700.HK")
    assert final["status"].tolist() == ["agree"] * 3
    assert final["yahoo_close_unadjusted"].tolist() == pytest.approx([100.0, 101.0, 102.0])

    # the second time download starts 7 days before the last saved date; when a raw traded price was changed the saved value is kept
    quotes.closes[days_ago(1)] = 999.0
    h = store.prices("0700.HK", days_ago(40).to_pydatetime())
    assert quotes.calls[-1] == days_ago(8)
    assert h.close[days_ago(1)] == 102.0

    # when the lookback range is enlarged, the earlier data is downloaded
    store.prices("0700.HK", days_ago(60).to_pydatetime())
    assert quotes.calls[-1] == days_ago(60)

    quotes.offline = yahoo.offline = True
    h = store.prices("0700.HK", days_ago(40).to_pydatetime())
    assert h.close.tolist() == [100.0, 101.0, 102.0]


def test_price_store_redownloads_yahoo_when_history_is_readjusted(tmp_path):
    quotes = FakeQuotes({days_ago(10): 100.0, days_ago(5): 100.0})
    yahoo = FakeYahoo({days_ago(10): 100.0, days_ago(5): 100.0})
    store = PriceStore(quotes, yahoo, tmp_path)
    store.prices("0700.HK", days_ago(20).to_pydatetime())

    # new ex-rights: Yahoo lowered all earlier prices
    yahoo.closes = {days_ago(10): 90.0, days_ago(5): 90.0, days_ago(1): 100.0}
    quotes.closes[days_ago(1)] = 100.0
    store.prices("0700.HK", days_ago(20).to_pydatetime())
    assert yahoo.calls[-1] == days_ago(20)
    assert saved(store, "0700.HK", "yahoo")["close"].tolist() == [90.0, 90.0, 100.0]


def test_price_store_adjusts_raw_prices_for_splits_in_memory(tmp_path):
    quotes = FakeQuotes({days_ago(10): 500.0, days_ago(5): 510.0, days_ago(1): 103.0})
    yahoo = FakeYahoo({days_ago(10): 100.0, days_ago(5): 102.0, days_ago(1): 103.0}, splits={days_ago(3): 5.0})
    store = PriceStore(quotes, yahoo, tmp_path)

    h = store.prices("0700.HK", days_ago(20).to_pydatetime())
    assert h.close.tolist() == pytest.approx([100.0, 102.0, 103.0])
    assert saved(store, "0700.HK", "tencent")["close"].tolist() == [500.0, 510.0, 103.0]

    yahoo.offline = True  # split records are saved and still take effect offline
    h = store.prices("0700.HK", days_ago(20).to_pydatetime())
    assert h.splits.to_dict() == {days_ago(3): 5.0}
    assert h.close.tolist() == pytest.approx([100.0, 102.0, 103.0])


def test_us_stock_uses_yahoo_only_and_is_not_split_adjusted_again(tmp_path):
    from valuation_monitor.market_calendar import US

    class UsYahoo(FakeYahoo):
        def price_history(self, symbol, start):
            h = super().price_history(symbol, start)
            return PriceHistory(h.close, h.splits, "USD", "America/New_York", bars=h.bars)

    # only one source: Tencent is not called; Yahoo's close is already split-adjusted and must not be divided again
    yahoo = UsYahoo({days_ago(10): 20.0, days_ago(5): 21.0, days_ago(2): 22.0}, splits={days_ago(3): 5.0})
    store = PriceStore(None, yahoo, tmp_path)
    h = store.prices("JD", days_ago(20).to_pydatetime(), US)
    assert h.close.tolist() == [20.0, 21.0, 22.0]
    assert h.currency == "USD" and h.timezone == "America/New_York"
    assert h.splits.to_dict() == {days_ago(3): 5.0}  # the split records are still used to adjust EPS
    assert saved(store, "JD", "tencent").empty
    final = store.final("JD")
    assert set(final["status"]) == {"yahoo_only"} and final["close"].tolist() == [20.0, 21.0, 22.0]

    yahoo.offline = True  # offline the saved data is used, and the currency is also saved with the Yahoo data
    h = store.prices("JD", days_ago(20).to_pydatetime(), US)
    assert h.close.tolist() == [20.0, 21.0, 22.0] and h.currency == "USD"


def test_earnings_store_parses_each_filing_once(tmp_path, monkeypatch):
    calls = []

    def fake_parse(filing, fmt, market, non_gaap=None):
        calls.append(filing.url)
        return Announcement(
            period=pd.Period("2026Q2", freq="Q"),
            published=filing.published,
            url=filing.url,
            currency="CNY",
            is_quarterly_table=True,
            values={"eps": [6.207, 6.115, 6.431], "non_ifrs_eps": [7.581, 6.931, 7.517]},
        )

    monkeypatch.setattr("valuation_monitor.store.parse_announcement", fake_parse)
    filing = Filing("t", pd.Timestamp("2026-08-12 16:31"), "https://example.com/a.pdf", b"")
    store = EarningsStore(tmp_path)

    first = store.load("0700.HK", [filing], pd.Series(dtype=float))
    second = store.load("0700.HK", [filing], pd.Series(dtype=float))
    assert calls == ["https://example.com/a.pdf"]
    assert second.quarters["eps"][pd.Period("2026Q2")].value == first.quarters["eps"][pd.Period("2026Q2")].value
    assert store.periods("0700.HK") is not None and store.ttm("0700.HK") is not None


# Freshly parsed filings are used exactly as the announcements table gives them back (store.as_saved)

def test_a_fresh_parse_is_used_as_it_is_stored():
    from valuation_monitor.filings import announcements_to_frame
    from valuation_monitor.market_calendar import get_market
    from valuation_monitor.store import as_saved

    parsed = Announcement(
        period=pd.Period("2017Q3", freq="Q"), published=pd.Timestamp("2017-11-22 11:27"), url="https://example.com/q3.htm", currency="USD",
        is_quarterly_table=False, values={"eps": [-0.75, 1.7630000000000001], "profit": [-132.68099999999998, 176.29999999999998]},
        market="US", shares_basis="year to date", note="a note", series_notes={"eps": "from the year-to-date figures"},
    )
    stored = as_saved(parsed, 7, get_market("US"))
    assert stored.values == {"eps": [-0.75, 1.763], "profit": [-132.681, 176.3]}  # 10 significant digits, as in the table
    assert (stored.period, stored.published, stored.url, stored.currency, stored.is_quarterly_table, stored.market, stored.shares_basis,
            stored.note, stored.series_notes, stored.supplement) == (parsed.period, parsed.published, parsed.url, parsed.currency, False, "US",
                                                                    "year to date", "a note", parsed.series_notes, False)
    assert announcements_to_frame([stored], 7).equals(announcements_to_frame([parsed], 7))  # the same row is written
    assert as_saved(stored, 7, get_market("US")) == stored  # nothing changes a second time
    supplement = as_saved(Announcement(pd.Period("2025Q4", freq="Q"), pd.Timestamp("2026-02-24 18:00"), "https://example.com/r.htm", "BRL", True,
                                       {"non_ifrs_eps": [2.0300000000000002]}, "US", supplement=True), 7, get_market("US"))
    assert supplement.supplement and supplement.values == {"non_ifrs_eps": [2.03]}


ROOT = Path(__file__).resolve().parent.parent


def earnings_tables(db_path):
    """The tables the earnings store writes, as sorted rows (the row order changes when a symbol is rewritten)."""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return {t: sorted(conn.execute(f'SELECT * FROM "{t}"'), key=repr) for t in ("announcements", "periods", "ttm")}


def full_dump(db_path):
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        tables = [t for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: sorted(conn.execute(f'SELECT * FROM "{t}"'), key=repr) for t in tables}


def real_monitor(tmp_path, formats=None):
    """The saved data offline, on a copy of the committed database; only the stocks with one of `formats` when given."""
    import shutil

    import yaml

    from valuation_monitor.config import parse_config
    from valuation_monitor.service import Monitor

    db = tmp_path / "valuation.db"
    shutil.copyfile(ROOT / "data" / "valuation.db", db)
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    config = parse_config(raw)
    if formats:
        keep = {s.symbol for s in config.stocks if s.filing_format in formats or s.sec_release_non_gaap}
        config = parse_config({**raw, "stocks": [s for s in raw["stocks"] if str(s["symbol"]) in keep]})
    return lambda: Monitor(config, ROOT / "data", tmp_path / "cache", offline=True, db_path=db), db


saved_data = pytest.mark.skipif(not (ROOT / "data" / "valuation.db").exists(), reason="no saved database")


@saved_data
def test_one_run_after_a_parser_bump_leaves_the_database_settled(tmp_path, monkeypatch):
    """After a PARSER_VERSIONS bump the run that parses again writes what every later run reads: a second run writes nothing.
    (Before store.as_saved the second run rewrote a few ttm values in their last digit, e.g. 1.7630000000000001 -> 1.763.)"""
    from valuation_monitor import filings

    monkeypatch.setitem(filings.PARSER_VERSIONS, "us_press_release", filings.PARSER_VERSIONS["us_press_release"] + 1)
    monkeypatch.setattr(filings, "SUPPLEMENT_PARSER_VERSION", filings.SUPPLEMENT_PARSER_VERSION + 1)  # XP's press releases too
    monkeypatch.setattr(store_module, "SUPPLEMENT_PARSER_VERSION", filings.SUPPLEMENT_PARSER_VERSION)
    monitor, db = real_monitor(tmp_path, {"us_press_release"})
    before = earnings_tables(db)
    monitor().analyze_all()
    first = full_dump(db)
    assert earnings_tables(db)["announcements"] != before["announcements"]  # the filings were parsed again (new parser version)
    monitor().analyze_all()
    assert full_dump(db) == first


@saved_data
def test_a_run_without_new_filings_parses_nothing_and_stays_settled(tmp_path, monkeypatch):
    """On the committed database, with no parser bump: nothing is parsed (so store.as_saved does not apply and the run is exactly
    what it was before it), and once run, the next run rewrites nothing in the earnings tables.

    The committed database itself need not be settled: a stock parsed by a scheduled run of the code before store.as_saved
    (Tesla when it was added) keeps last-digit values in its ttm rows until the next run rewrites them once."""

    def no_parse(*args, **kwargs):
        raise AssertionError("nothing should be parsed again")

    monkeypatch.setattr(store_module, "parse_announcement", no_parse)
    monitor, db = real_monitor(tmp_path)
    reports = monitor().analyze_all()
    assert not [r.error for r in reports if r.error and "nothing should be parsed" in r.error]
    settled = earnings_tables(db)
    monitor().analyze_all()
    assert earnings_tables(db) == settled
