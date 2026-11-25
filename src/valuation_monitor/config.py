"""Load and validate config.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from valuation_monitor.market_calendar import MARKETS


@dataclass(frozen=True)
class Level:
    upper: float  # upper percentile bound (inclusive), 0-100
    label: str


FILING_SOURCES = {
    "HK": "HKEXnews",
    "US": "SEC EDGAR",
    "KR": "Samsung Electronics IR website (English financial statements)",
    "CN": "CNINFO (the CSRC-designated disclosure platform)",
}


EPS_BASES = ("basic", "diluted")  # the EPS a PE metric is based on: basic whenever the company discloses it, diluted only when it does not


@dataclass(frozen=True)
class StockConfig:
    symbol: str  # Yahoo Finance symbol, e.g. 0700.HK, JD
    name: str
    metrics: list[str] = field(default_factory=lambda: ["pe"])
    filing_format: str = "highlights"  # layout of the earnings filings, see filings.FILING_FORMATS
    market: str = "HK"  # listing market: HK (HKEXnews), US (SEC EDGAR), KR (company IR website) or CN (A-shares, CNINFO)
    cik: int | None = None  # SEC Central Index Key of a US-listed company
    sec_exhibit: str = "release"  # what is parsed: the earnings press release itself, the summary US GAAP financial statements exhibit (ASML), the IFRS consolidated financial statements (XP) or SAP's quarterly results document (sap_ifrs)
    sec_form: str = "6-K"  # the SEC form that carries the earnings press release: 6-K (foreign private issuers, e.g. JD, ASML) or 8-K item 2.02 (US companies, e.g. Meta)
    eps_basis: dict[str, str] = field(default_factory=dict)  # PE metric -> "diluted" where the company discloses no basic EPS (Alibaba: pe_non_gaap); others are basic
    sec_release_non_gaap: bool = False  # also read the earnings press release for its adjusted EPS, next to the statements (XP; see release_eps.py)
    non_gaap_changes: tuple[str, ...] = ()  # days (YYYY-MM-DD) the company changed the definition of its Non-GAAP EPS (Cognizant: 2019-02-06; see build_history)

    def basis(self, metric) -> str | None:
        """The EPS basis of a PE metric ("basic" or "diluted"); None for a metric not based on EPS (PS, PB)."""
        return self.eps_basis.get(metric.key, "basic") if metric.eps_based else None

    def metric_label(self, metric) -> str:
        """The name shown for a metric of this stock: a PE says whether it is on basic or diluted EPS, e.g. "Basic PE (TTM)", "Diluted PE Non-GAAP (TTM)"."""
        basis = self.basis(metric)
        return f"{basis.capitalize()} {metric.label}" if basis else metric.label

    @property
    def filing_source(self) -> str:
        return FILING_SOURCES[self.market]

    @property
    def cross_checks_prices(self) -> bool:
        """Whether prices from two or more sources can be cross-checked (HK: Tencent and Yahoo; US: Yahoo only)."""
        return MARKETS[self.market].cross_checks_prices


DEFAULT_LOOKBACK_OPTIONS = [10, 5, 3, 2, 1]


DEFAULT_MAX_PRICE_AGE_DAYS = {"HK": 5, "US": 5, "KR": 6, "CN": 10}  # long enough for weekends and the longest holidays (China's Golden Week and New Year)


@dataclass(frozen=True)
class Freshness:
    """How old saved data may be before the update job (and the scheduled workflow) fails: see freshness.py."""

    max_price_age_days: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MAX_PRICE_AGE_DAYS))  # by market
    max_fx_age_days: int = 7
    max_filing_age_days: int = 200  # only a warning: companies publish at least every half year, and nobody fixes a late filing


@dataclass(frozen=True)
class Config:
    stocks: list[StockConfig]
    lookback_years: int = 10  # default selected lookback in years
    levels: list[Level] = field(default_factory=list)
    lookback_options: list[int] = field(default_factory=lambda: list(DEFAULT_LOOKBACK_OPTIONS))  # lookback years selectable on the web page
    freshness: Freshness = field(default_factory=Freshness)

    @property
    def max_lookback(self) -> int:
        """The longest lookback in years; it decides how many years of prices and earnings are prepared."""
        return max(self.lookback_options)


DEFAULT_LEVELS = [
    Level(10, "Deeply undervalued"),
    Level(30, "Undervalued"),
    Level(70, "Fair"),
    Level(90, "Overvalued"),
    Level(100, "Deeply overvalued"),
]


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return parse_config(raw)


def parse_config(raw: dict) -> Config:
    from valuation_monitor.filings import FILING_FORMATS
    from valuation_monitor.metrics import METRICS

    stocks = []
    for s in raw.get("stocks") or []:
        metrics = [m.lower() for m in s.get("metrics", ["pe"])]
        unknown = [m for m in metrics if m not in METRICS]
        if unknown:
            raise ValueError(f"{s['symbol']}: unknown metric {unknown}; available metrics: {sorted(METRICS)}")
        symbol = str(s["symbol"])
        suffix_markets = {".HK": "HK", ".KS": "KR", ".KQ": "KR", ".SZ": "CN", ".SS": "CN"}
        default_market = next((m for suffix, m in suffix_markets.items() if symbol.upper().endswith(suffix)), "US")
        market = str(s.get("market", default_market)).upper()
        if market not in MARKETS:
            raise ValueError(f"{symbol}: unknown market {market}; available markets: {sorted(MARKETS)}")
        # HK earnings come from HKEXnews filings (PDF), US from SEC EDGAR press releases (HTML), KR from the company IR website financial statements
        default_format = {"HK": "highlights", "US": "press_release", "KR": "kifrs_statements", "CN": "cn_report"}[market]
        filing_format = str(s.get("filing_format", default_format))
        if filing_format not in FILING_FORMATS:
            raise ValueError(f"{symbol}: unknown filing format {filing_format}; available formats: {sorted(FILING_FORMATS)}")
        foreign_formats = {"press_release": "US", "us_press_release": "US", "us_gaap_statements": "US", "ifrs_statements": "US", "sap_ifrs": "US", "kifrs_statements": "KR", "cn_report": "CN"}  # non-HK formats only apply to their own market
        if foreign_formats.get(filing_format, "HK" if market == "HK" else None) != market:
            raise ValueError(f"{symbol}: filing format {filing_format} does not match market {market}")
        cik = int(s["cik"]) if s.get("cik") is not None else None
        if market == "US" and cik is None:
            raise ValueError(f"{symbol}: US stocks need a cik (the company's SEC Central Index Key)")
        sec_form = str(s.get("sec_form", "6-K")).upper()
        if sec_form not in ("6-K", "8-K"):
            raise ValueError(f"{symbol}: sec_form must be 6-K or 8-K, got {sec_form}")
        sec_exhibit = str(s.get("sec_exhibit", "release"))
        if sec_exhibit not in ("release", "us_gaap_statements", "ifrs_statements", "sap_ifrs"):
            raise ValueError(f"{symbol}: sec_exhibit must be release, us_gaap_statements, ifrs_statements or sap_ifrs, got {sec_exhibit}")
        sec_release_non_gaap = bool(s.get("sec_release_non_gaap", False))
        if sec_release_non_gaap and sec_exhibit != "ifrs_statements":
            raise ValueError(f"{symbol}: sec_release_non_gaap reads the press release next to the IFRS statements (sec_exhibit: ifrs_statements), got {sec_exhibit}")
        non_gaap_changes = tuple(str(d) for d in s.get("non_gaap_changes") or ())
        for day in non_gaap_changes:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError(f"{symbol}: non_gaap_changes are days written YYYY-MM-DD, got {day}")
        if non_gaap_changes and "pe_non_gaap" not in metrics:
            raise ValueError(f"{symbol}: non_gaap_changes is for a stock with pe_non_gaap")
        eps_basis = {str(k).lower(): str(v).lower() for k, v in (s.get("eps_basis") or {}).items()}
        for key, basis in eps_basis.items():
            if key not in metrics or not METRICS[key].eps_based:
                raise ValueError(f"{symbol}: eps_basis is for the PE metrics of the stock ({[m for m in metrics if METRICS[m].eps_based]}), got {key}")
            if basis not in EPS_BASES:
                raise ValueError(f"{symbol}: eps_basis of {key} must be one of {list(EPS_BASES)}, got {basis}")
        stocks.append(
            StockConfig(
                symbol=symbol,
                name=str(s.get("name", symbol)),
                metrics=metrics,
                filing_format=filing_format,
                market=market,
                cik=cik,
                sec_form=sec_form,
                sec_exhibit=sec_exhibit,
                eps_basis={k: v for k, v in eps_basis.items() if v != "basic"},
                sec_release_non_gaap=sec_release_non_gaap,
                non_gaap_changes=non_gaap_changes,
            )
        )
    if not stocks:
        raise ValueError("The config has no stocks (stocks is empty)")

    levels = [Level(float(lv["upper"]), str(lv["label"])) for lv in raw.get("levels") or []]
    levels = sorted(levels, key=lambda lv: lv.upper) or DEFAULT_LEVELS
    if levels[-1].upper < 100:
        raise ValueError("The last entry of levels must have upper = 100")

    options = sorted({int(y) for y in raw.get("lookback_options") or DEFAULT_LOOKBACK_OPTIONS}, reverse=True)
    if options[-1] < 1:
        raise ValueError("The years in lookback_options must be positive integers")
    default = int(raw.get("lookback_years", options[0]))
    if default not in options:
        raise ValueError(f"lookback_years={default} must be one of lookback_options {options}")
    return Config(stocks=stocks, lookback_years=default, levels=levels, lookback_options=options, freshness=parse_freshness(raw.get("freshness") or {}))


def parse_freshness(raw: dict) -> Freshness:
    prices = {**DEFAULT_MAX_PRICE_AGE_DAYS, **{str(m).upper(): int(d) for m, d in (raw.get("max_price_age_days") or {}).items()}}
    unknown = sorted(set(prices) - set(MARKETS))
    if unknown:
        raise ValueError(f"freshness.max_price_age_days: unknown market {unknown}; available markets: {sorted(MARKETS)}")
    fx, filings = int(raw.get("max_fx_age_days", 7)), int(raw.get("max_filing_age_days", 200))
    if min(prices.values()) < 1 or fx < 1 or filings < 1:
        raise ValueError("The freshness limits must be positive numbers of days")
    return Freshness(max_price_age_days=prices, max_fx_age_days=fx, max_filing_age_days=filings)
