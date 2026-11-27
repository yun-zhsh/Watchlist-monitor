"""Arrange the raw data of each source into the form the metric calculations need."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from valuation_monitor.config import Config, StockConfig
from valuation_monitor.filings import EarningsHistory
from valuation_monitor.market_calendar import get_market
from valuation_monitor.providers.advantech import AdvantechIrProvider
from valuation_monitor.providers.cninfo import CninfoProvider
from valuation_monitor.providers.cvm import CvmProvider
from valuation_monitor.providers.hkex import HkexProvider
from valuation_monitor.providers.samsung import SamsungIrProvider
from valuation_monitor.providers.sec import SecProvider
from valuation_monitor.providers.xj_storage import XjStorageProvider
from valuation_monitor.store import EarningsStore, PriceStore


@dataclass
class StockData:
    stock: StockConfig
    close: pd.Series  # closing price on trading days, in the price currency
    price_currency: str
    fx: pd.Series  # same index as close: 1 unit of reporting currency = ? units of price currency
    fundamentals: pd.DataFrame  # report_date, available_date, eps_ttm, non_ifrs_eps_ttm (reporting currency)
    earnings: EarningsHistory | None = None


class DataLoader:
    def __init__(
        self, config: Config, prices: PriceStore, hkex: HkexProvider, sec: SecProvider, samsung: SamsungIrProvider, cninfo: CninfoProvider, earnings: EarningsStore,
        xj_storage: XjStorageProvider | None = None, advantech: AdvantechIrProvider | None = None, cvm: CvmProvider | None = None,
    ):
        self.config = config
        self.prices = prices
        self.hkex = hkex
        self.sec = sec
        self.samsung = samsung
        self.cninfo = cninfo
        self.earnings = earnings
        self.xj_storage = xj_storage
        self.advantech = advantech
        self.cvm = cvm

    def load(self, stock: StockConfig) -> StockData:
        # fetch one extra year of prices and two extra years of filings so the start of the lookback window already has 4 quarters for the TTM
        now = datetime.now()
        market = get_market(stock.market)
        prices = self.prices.prices(stock.symbol, now - pd.DateOffset(years=self.config.max_lookback + 1), market)
        since = now - pd.DateOffset(years=self.config.max_lookback + 2)
        if stock.market == "US":
            filings = self.sec.results_announcements(stock.symbol, stock.cik, since, stock.sec_form, stock.sec_exhibit)
            if stock.sec_release_non_gaap:  # the press release as well, for its adjusted EPS (XP)
                filings += self.sec.results_announcements(stock.symbol, stock.cik, since, stock.sec_form, supplement=True)
            if stock.sec_exhibit == "tesla_update":  # the Form 10-Q or 10-K of the quarters whose Update has its statements as page images only
                filings += self.sec.results_announcements(stock.symbol, stock.cik, since, "10-Q/10-K", "periodic_report")
        elif stock.market == "KR":
            filings = self.samsung.results_announcements(stock.symbol, since)
        elif stock.market == "CN":
            filings = self.cninfo.results_announcements(stock.symbol, since)
        elif stock.market == "JP":
            filings = self.xj_storage.results_announcements(stock.symbol, since)
        elif stock.market == "TW":
            filings = self.advantech.results_announcements(stock.symbol, since)
        elif stock.market == "BR":
            filings = self.cvm.results_announcements(stock.symbol, since)
        else:
            filings = self.hkex.results_announcements(stock.symbol, since)
        non_gaap = stock.eps_basis.get("pe_non_gaap", "basic") if "pe_non_gaap" in stock.metrics else None  # which Non-GAAP EPS the parser reads, if any
        earnings = self.earnings.load(stock.symbol, filings, prices.splits, prices.close.index, stock.filing_format, market, non_gaap, stock.non_gaap_changes,
                                     stock.fiscal_year_end)

        if earnings.currency == prices.currency:
            fx = pd.Series(1.0, index=prices.close.index)
        else:
            raw_fx = self.prices.fx(earnings.currency, prices.currency, prices.close.index[0])
            fx = raw_fx.reindex(raw_fx.index.union(prices.close.index)).sort_index().ffill()
            fx = fx.reindex(prices.close.index)

        return StockData(
            stock=stock,
            close=prices.close,
            price_currency=prices.currency,
            fx=fx,
            fundamentals=earnings.ttm,
            earnings=earnings,
        )
