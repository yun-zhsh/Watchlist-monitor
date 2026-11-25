# Price sources: design

Status: **designed, not built; on hold.** The maintainer has not yet decided when (or whether) to build it. Nothing of it is in the
code. This document specifies how the daily closing prices of the configured stocks should be sourced, cross-checked, stored and
shown, after an external validation of the MCP server (sampling Samsung Electronics, 005930.KS) found a closing price that does not
match the exchange. It is written so that a developer (or a new coding session) who has read [CLAUDE.md](../CLAUDE.md), this file and
[trading-calendar.md](trading-calendar.md) can build it without further discussion. Decisions already made are in section 2; the
points left to the implementer are in section 15; the questions still open for the maintainer, which must be answered **before** the
first line of code (except the backtest of section 14, PR 1, which exists to answer one of them), are in section 19.

Contents: 1 Purpose · 2 Decisions made · 3 How prices work today · 4 Scope · 5 The Samsung finding · 6 Sources by market ·
7 Price basis: unadjusted closes, splits and bonus shares · 8 Cross-check rule and tolerances · 9 Data and schema changes ·
10 Samsung preferred shares (PS and PB basis) · 11 MCP tool `get_prices` · 12 Regression and migration · 13 How this fits with the
trading calendar · 14 Build plan (pull requests) · 15 Left to the implementer · 16 Facts found while designing · 17 Risks ·
18 Not in scope · 19 Open questions for the maintainer and how to start

## 1. Purpose

A historical percentile of PE, PS or PB is only as good as the closing prices it is built from. Every day of the window enters the
distribution, so a systematic error in one market's prices moves both today's value and its percentile. The project's rule for
financials is "original filings, never a third party"; prices cannot follow it literally (exchange data is paid or behind a login),
but they can follow its spirit:

* the price of a day is the **closing price of the primary exchange's regular session** (KRX, SSE/SZSE, HKEX, NYSE/Nasdaq), unadjusted
  for dividends, on the share basis of that day;
* it comes from a source whose basis is known, and, wherever a free second source exists, it is **cross-checked** day by day;
* every price shown or used says where it came from and whether it was confirmed;
* a change of source is a data migration: its effect on every historical percentile is measured and explained before it ships.

## 2. Decisions made

Recorded from the maintainer's review of the external validation report (2026-11-25). The report is a lead, not a specification;
where it and this list differ, this list wins.

1. **HK is already cross-checked; nothing changes there.** Tencent (unadjusted) and Yahoo are consolidated by `prices.py` with a 0.2%
   tolerance and the statuses `agree`, `mismatch`, `tencent_only`, `yahoo_only`. No AKShare for HK. Today only the **US, KR and CN**
   stocks are single-source (section 3).
2. **Unadjusted prices and split handling already exist** and are correct (section 3 and 7). A separate corporate-action event table is
   optional and later. A one-off **historical check of every A-share bonus share and capital-reserve transfer** against the company's
   own announcements is worth a step (section 7, PR 3).
3. **CN:** not AKShare as the primary source (a heavy dependency; its Eastmoney endpoints are often unreachable from servers abroad, such
   as GitHub runners and Cloud Run). Extend the existing **Tencent provider to A-shares** as the second source; Baostock is a fallback
   option only.
4. **KR:** not pykrx (it requires pandas < 3, while this project runs pandas 3; it needs KRX account secrets in GitHub and Cloud Run,
   with lockouts after failed logins and password expiry; and its stock prices reportedly come from Naver anyway). Candidates: Naver's
   daily prices directly (terms, robots and the exact basis to be checked: section 6.1) or another source on the KRX basis. **Step 1 of
   the whole plan is a backtest** (section 14, PR 1) with deviation statistics, before any source switch; the switch is the
   maintainer's decision, based on that report.
5. **BABA and JD have no dual-class issue:** the fixed ADS ratio is applied to both price and per-share figures. The **Samsung
   preferred-share basis** of PS and PB is an open question (section 10, question Q2).
6. **Coordinate with the trading calendar** ([trading-calendar.md](trading-calendar.md), sections 9 and 10): section 13 says how the two
   designs fit and in which order to build them. That document is not changed by this one.
7. **Keep the 0.2% agreement tolerance** unless there is a reason; the report's 0.1% / 1% tiers are discussed against tick sizes in
   section 8 (question Q4).
8. A **`get_prices` MCP tool** follows the MCP rules of [CLAUDE.md](../CLAUDE.md): read-only, offline, `as_of`, sources in the
   result, no arithmetic outside Python, tests in `tests/test_mcp_server.py` and `tests/test_mcp_production.py` (section 11).
9. **A primary-source change changes historical prices and therefore percentiles:** a full before/after regression for all configured
   stocks is required, with every difference recorded and explained (section 12).
10. Open questions go to the maintainer with a recommendation (section 19); this document does not decide them.

## 3. How prices work today

From the code as of `main` at 0.4.3 (`prices.py`, `store.py`, `providers/tencent.py`, `providers/yahoo.py`, `market_calendar.py`,
`db.py`, `service.py`, `mcp_server.py`):

* **Which markets have two sources.** `Market.cross_checks_prices` (`market_calendar.py`) is `True` only for `HK`. `PriceStore.prices`
  (`store.py`) downloads Tencent bars only when it is true; for US, KR and CN it passes an empty Tencent frame to `consolidate`.
* **Tencent provider.** `TencentQuoteProvider` calls `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get` with
  `param=hk<code>,day,<from>,<to>,400,` (one request per calendar year) and reads the `day` rows (date, open, close, high, low,
  volume). These are unadjusted traded prices. The code is HK-only: the symbol is built as `hk` + the HKEX stock code, and `currency` is
  fixed to `HKD`. Tencent bars are saved with `frozen=True` (saved history is never changed).
* **Yahoo provider.** `YahooProvider.price_history` calls the v8 chart API with `events=split`. Yahoo's `close` is adjusted for splits
  and (for HK) for distributions in kind; `adjclose` is further adjusted for cash dividends and is saved but not used. Split records are
  saved in the meta document `prices/<symbol>/yahoo`. Yahoo bars are saved with `frozen=False`: when Yahoo changes saved history by more
  than `MISMATCH_TOLERANCE = 1e-4`, the whole history is downloaded again.
* **Consolidation** (`prices.consolidate`): zero-volume rows are invalid; Yahoo's adjustment factor is estimated per day from the ratio
  of the two sources; the unadjusted Yahoo close is compared with Tencent's with `AGREE_TOLERANCE = 0.002` (0.2%); the final close is
  Tencent's, Yahoo's only on days Tencent is missing. Statuses: `agree`, `mismatch`, `tencent_only`, `yahoo_only`
  (`PRICE_STATUS_LABELS` in `service.py`: "Agree", "Mismatch", "Tencent only", "Yahoo only").
* **Single-source markets.** With no Tencent data, every day is `yahoo_only`, `source = yahoo`, the factor is 1.0, and the final close
  is Yahoo's close as served: **split-adjusted to today's share basis**, not adjusted for dividends. For HK, by contrast, the final
  Tencent close is unadjusted and `split_adjust` converts it to today's basis afterwards. In both cases the per-share figures of the
  filings are converted to the same basis by the split records (`filings.split_factor`), so price and EPS stay consistent: Samsung's
  2018-05-04 50:1 split gives a 2016 Q2 basic EPS of 40,904 / 50 = 818.08 KRW against a Yahoo close that is likewise divided by 50.
* **Storage** (`db.py`): `price_bars` (symbol, source = `tencent` | `yahoo`, date, OHLC, `adjclose`, volume), `prices_final`
  (symbol, date, `close`, `source`, `status`, `tencent_close`, `yahoo_close_unadjusted`, `yahoo_factor`), `meta`. `SCHEMA_VERSION = 1`.
  A day is saved only once it is over in the market's time zone (`local_today`); `prices_final` is rewritten with `prune=True`.
* **What the MCP server says.** `get_valuation` returns `sources.price = {day, close, currency, source, cross_check}`, where
  `cross_check` is the saved `status`; for every US, KR and CN stock it is `yahoo_only`. There is no tool that returns prices alone.
* **What the README says.** "Data sources" and "Known limitations" state that US, Korean and A-share prices have Yahoo as the only
  source, with the reason "Tencent has no usable API for them". Section 16 shows that this reason is no longer true for A-shares and
  US stocks (it is true for Korea).

Saved data (`data/valuation.db`, read-only, 2026-11-25): 18 configured stocks; the 2 HK stocks are cross-checked (0700.HK: 2716
`agree`, 10 `tencent_only`; 2382.HK: 2713 `agree`, 12 `tencent_only`; no `mismatch`), the other 16 are `yahoo_only` for every day
(10 US, 1 KR, 5 CN).

## 4. Scope

In scope:

* KR: find out how far the saved Samsung closes are from the KRX regular-session close (backtest), then, if the maintainer decides so,
  change the primary source and add a cross-check.
* CN: Tencent as a second source for the five A-shares, consolidated like HK.
* US: whether to add Tencent (or another source) as a second source (question Q3).
* A one-off check of the A-share bonus share and transfer history.
* The PS and PB share basis of Samsung (question Q2).
* An MCP tool that returns the saved daily prices with their sources and cross-check status.
* The regression and migration rules for any change of a primary source.

The trading calendar, FX rates and the split records themselves (other than checking them) are not in scope (section 18).

## 5. The Samsung finding

**What the report says.** For 2026-11-19 the MCP returned Samsung's close as 196,900 KRW (Yahoo). The report gives the KRX official close
as 195,600, high 196,300, low 189,800 (from Naver and a news report), and notes that Yahoo's high of 196,900 is above that KRX high, so the
Yahoo bar probably includes trades on Nextrade (NXT, the Korean alternative trading venue with pre- and after-market sessions until about
20:00, launched in March 2025) or a consolidated last price. 2026-02-13 (181,200) matched. Its hypothesis: every day since the NXT launch
may deviate; only one day was confirmed.

**What was checked while writing this document** (2026-11-25; read-only queries of `data/valuation.db`, and Naver's public JSON
endpoints fetched once into a scratch directory, nothing written to `data/`):

* Saved Yahoo bar for 2026-11-19: open 196,000, high 196,900, low 189,800, close 196,900, volume 12,836,690.
* Naver `siseJson` (and `m.stock.naver.com/api/stock/005930/price`) for the same day: open 196,000, high 197,600, low 189,800,
  **close 197,400**, volume 12,836,690. So Naver's own daily endpoint gives neither the report's 195,600 nor Yahoo's 196,900. The volume
  is identical to Yahoo's to the share, which suggests both carry the same KRX volume and differ only in the close. Which of the three
  closes is the KRX regular-session close is **not verified**.
* Over 2024-01-02 to 2026-11-24, 704 days are in both sources:

  | Period | Days | Close identical | Within 0.2% | Within 1% | Largest difference | Volume identical |
  | --- | --- | --- | --- | --- | --- | --- |
  | 2024-01-02 to 2025-02-28 (before NXT) | 282 | 279 | 279 | 281 | 2.47% (2024-10-14) | 281 |
  | 2025-03-04 to 2026-11-24 (NXT era) | 422 | 376 | 379 | 406 | 3.55% | 421 |

  Days with a different close by month: one each in 2024-02, 2024-10, 2024-11, 2025-09, 2025-10, 2026-03 and 2026-04, then **9 in
  2026-09, 17 in 2026-10 and 16 in 2026-11** (up to 2026-11-24). Naver has one day (2025-09-19) that Yahoo lacks.
* So the hypothesis "every day since the NXT launch" is not what this sample shows: the two sources agreed on almost every day until
  August 2026 and disagree on most days since September 2026. Possible explanations, **none verified**: a change in how Yahoo builds
  the KR daily bar (for example taking a last price after the regular session); the update job's timing (the day is saved only after
  midnight Seoul time, after NXT has closed, and re-downloaded if Yahoo later changes it); or a change at Naver. Some saved Yahoo closes
  are not multiples of 100 KRW (89,750 on 2025-10-02, 189,650 on 2026-04-01), and Naver shows a high of 199,950 on 2026-11-18; whether
  these are valid KRX prices depends on the KRX tick table in force, which was not verified.
* `m.stock.naver.com/api/stock/005930/basic` reports the regular close (`closePrice`) and, separately, `overMarketPriceInfo` with
  `tradingSessionType: AFTER_MARKET` (the NXT after-market price). This suggests Naver keeps the KRX regular close apart from the NXT
  price, but whether its historical `siseJson` close is the KRX-only close was **not verified**.

**Consequence for the design.** The first step cannot be "switch to Naver": it must establish, from an authoritative reference, which
source carries the KRX regular-session close, over the whole history (section 14, PR 1).

## 6. Sources by market

### 6.1 KR (Samsung Electronics, 005930.KS)

| Candidate | Basis | Access | Notes |
| --- | --- | --- | --- |
| Yahoo (today) | unknown since 2026-09 (section 5) | free, no key | split-adjusted close; 50:1 split record of 2018-05-04 |
| Naver `siseJson` / `m.stock.naver.com` daily prices | believed KRX regular close, **not verified** | free, no key | unadjusted; `robots.txt` of `api.finance.naver.com`, `finance.naver.com` and `m.stock.naver.com` is `Disallow: /` for all agents (checked 2026-11-25); terms of use not checked |
| KRX Data Marketplace (data.krx.co.kr) | official | free account, login (reportedly required now, not verified) | the authoritative reference; account secrets, lockout and expiry burden (decision 4) |
| pykrx | as its upstream | KRX login | rejected (decision 4) |
| Tencent | n/a | the `kr005930` code returns a quote but **no daily bars** (checked 2026-11-25) | not usable |

Recommendation (question Q1): use the backtest to decide. As the **reference** for the backtest (not as a run-time source), the
maintainer downloads Samsung's daily closes for the backtest period once by hand from the KRX Data Marketplace with their own account,
and the file is used only to score the candidates; no KRX credentials go into the repository, GitHub or Cloud Run. If Naver's close equals
KRX's on all reference days, Naver is the best run-time candidate on accuracy, but its `robots.txt` disallows automated access, so
using it is the maintainer's call on terms, not a technical one. If it is not acceptable, the fallback is to keep Yahoo as the run-time
source and flag the days it disagrees with a second source as `mismatch` in the cross-check, so that at least the MCP and the web page
say so.

### 6.2 CN (Hikvision, Glodon, Zhongji Innolight, China Northern Rare Earth, Bank of Nanjing)

The Tencent endpoint already used for HK serves A-shares with the codes `sz<6 digits>` and `sh<6 digits>` (checked 2026-11-25 for
`sz002415`, `sz300308`, `sh600111` and `sh601009`; Glodon, `sz002410`, was not fetched, with history back to at least 2015-10-28 for `sz002415`):

* The `day` rows are **unadjusted**: Hikvision closes 32.75 on 2016-05-17 and 20.78 on the ex-date 2016-05-18, and the ex-date row carries
  the event (`cqr` 2016-05-18, "per 10 shares: RMB 7 cash, 3 bonus shares, 2 transfer shares", ratio 1.5). Yahoo's saved closes for the
  same days (14.5556, 13.8533) are exactly these divided by the later split factors (1.5 x 1.5 and 1.5), so Yahoo's A-share close is
  split-adjusted only, not dividend-adjusted, and consistent with Tencent.
* For 2026-11-17 to 2026-11-20 the Tencent closes equal the saved Yahoo closes for all four stocks checked (to the cent).
* The A-share symbols in `config.yaml` end in `.SZ` and `.SS`; Tencent uses `sz` and `sh` prefixes.

Recommendation: make CN work exactly like HK: Tencent primary (unadjusted), Yahoo second, `consolidate` unchanged, `split_adjust` with
Yahoo's split records, `cross_checks_prices=True` for `CN`. The Tencent event records are a free cross-check of Yahoo's split records
(section 7). Baostock stays a documented fallback only (it uses its own socket protocol, not HTTPS; whether it is reachable through the
cloud proxy and from GitHub runners was **not verified**). AKShare is not used (decision 3).

### 6.3 HK (Tencent Holdings, Sunny Optical)

No change (decision 1). Tencent primary, Yahoo second, 0.2% tolerance, as today.

### 6.4 US (Meta, Cognizant, Amphenol, Dynatrace, SAP, ASML, XP, Sea, Alibaba, JD)

Tencent serves US stocks too, but only with an exchange suffix: `usMETA.OQ` (Nasdaq) and `usAPH.N` (NYSE) return daily bars, `usMETA`
alone returns a quote and no bars (checked 2026-11-25). The bars are **unadjusted** and carry split events: Amphenol 160.08 on 2026-09-02 and
82.07 on 2026-09-03 with the event "every 1 share split into 2", and 50.87 on 2016-01-04, which equals the saved Yahoo close 6.3587 times 8
(the 2021, 2024 and 2026 2:1 splits). Meta's Tencent closes for 2026-11-17 to 2026-11-20 equal the saved Yahoo closes. History back to at
least 2016-01-04. The suffix of each stock (`.OQ`, `.N`, possibly others such as `.AM`) must be configured or looked up; the rules were
not verified for all ten stocks.

US prices have not shown a problem, and the US consolidated close and the primary listing's official close are the same number for
these stocks in normal conditions (the closing auction of the listing exchange). Recommendation (question Q3): add Tencent as the US
second source, with Yahoo staying primary for US (the reverse of HK and CN), so that the saved US history does not change; a backtest of
the ten US stocks comes first, and if it shows nothing to gain, US stays single-source and the reason is recorded here.

### 6.5 Not changed

FX rates (`fx_rates`, Yahoo `XXXYYY=X`) and split records (Yahoo `events=split`) keep their source; split records get a cross-check from
the Tencent events (section 7).

## 7. Price basis: unadjusted closes, splits and bonus shares

The report recommends unadjusted closes, never forward- or backward-adjusted (`qfq` / `hfq`) prices, and splits and bonus shares
handled with the same factor for price and per-share figures. **The code already does this** (section 3): HK and, after this design, CN
use unadjusted Tencent closes converted to today's basis by the split records; Yahoo-only markets use Yahoo's split-adjusted close,
which is the same thing (verified for Samsung and Hikvision in sections 3 and 6.2). Dividend adjustment is never applied (`adjclose` is not
used). This stays.

What is worth adding:

* **A one-off check of every A-share bonus share and transfer** (PR 3): for each of the five A-shares, list every ex-date with a share
  distribution from three places: Yahoo's saved split records, the Tencent event records (`FHcontent`, `cqr`), and the company's own
  distribution announcement on CNINFO (the ratio and the ex-date). Saved Yahoo split records as of 2026-11-25: 002415.SZ 1.5 on
  2016-05-18 and 2017-05-16; 002410.SZ 1.4 on 2023-05-15; 300308.SZ 1.4 on 2019-06-04 and 2024-06-06; 601009.SS 1.8 on 2016-06-07 and 1.4 on
  2017-07-18; 600111.SS none. Every difference is either fixed (a missing or wrong split record changes per-share figures before that
  date, so it needs the regression of section 12) or explained in the findings. The result is recorded in this document's section 16 and
  in the README if it changes data.
* **An ongoing check** in the update job: a Tencent event with a share ratio that has no Yahoo split record on the same ex-date (or the
  reverse) is a warning, and for the cross-checked markets, a jump of the estimated Yahoo factor that coincides with no split record is
  one too.
* **An event table** (`symbol, ex_date, ratio, source_url`) is optional and later (decision 2). It becomes useful only if the Yahoo split
  records turn out to be wrong or incomplete; then it overrides them, with the source of every row.

## 8. Cross-check rule and tolerances

Today: one tier, `AGREE_TOLERANCE = 0.002` (0.2%), statuses `agree` / `mismatch` / single-source.

The report proposes: difference up to 0.1% = match; up to 1% = minor mismatch (warning); over 1% = mismatch for manual review.

Against tick sizes (the smallest price step): two sources quoting the same official close should agree exactly; a difference of one
tick means one of them is not the official close (a last trade, a different session, a rounding). A tolerance only needs to absorb
rounding by a source (Yahoo's floats, for example 6.3587 for 50.87 / 8) and the factor estimation of `consolidate`. One tick as a
share of the price, for typical prices of the stocks here (tick tables **not verified** for the current year; HKEX and KRX have changed
theirs recently):

| Market | Example price | Tick (believed) | One tick |
| --- | --- | --- | --- |
| KR | 196,900 KRW | 100 KRW | 0.05% |
| CN | 13.25 RMB (Bank of Nanjing) / 408.40 RMB (Innolight) | 0.01 RMB | 0.08% / 0.002% |
| HK | about 500 HKD (Tencent) / about 70 HKD (Sunny) | 0.5 HKD / 0.05 HKD | 0.1% / 0.07% |
| US | 82 USD (Amphenol) | 0.01 USD | 0.01% |

So 0.1% is above one tick for most of these stocks but not for Tencent at 0.5 HKD (exactly one tick), and 0.2% covers one to four ticks.
A one-tick difference is a real disagreement about which price is official, which is what the cross-check exists to find, so a wider
tolerance hides exactly the KR case of section 5 (196,900 vs 197,400 is 0.25%).

Recommendation (question Q4): keep **0.2%** as the `agree` threshold for compatibility with the saved HK statuses (changing it rewrites
those statuses), and add the report's upper tier as a second status rather than replacing the first: `agree` up to 0.2%, `mismatch` above
it, and a warning in the MCP answer and the run log when a day used for a valuation is a `mismatch` of more than 1%. Whether the
`agree` threshold should be tightened to "same tick" per market is the maintainer's decision; it needs the tick tables first.

## 9. Data and schema changes

* `price_bars.source` gains values (`tencent` for CN and US already fits; `naver` or another name for KR if chosen). No column change.
* `prices_final` has HK-specific column names (`tencent_close`, `yahoo_close_unadjusted`, `yahoo_factor`). For CN on Tencent they fit
  as they are. For a KR source other than Tencent they do not. Two options, left to the implementer (section 15) unless the maintainer
  has a preference: (a) keep the columns and give them a general meaning (`tencent_close` becomes "primary close", documented), or (b)
  rename them to `primary_close`, `secondary_close_unadjusted`, `secondary_factor` with `primary_source` / `secondary_source`, which
  needs `SCHEMA_VERSION = 2` in `db.py` and a migration (SQLite `ALTER TABLE ... RENAME COLUMN`, no rebuild, no vacuum; `CLAUDE.md`).
  (b) is cleaner; it is a one-time rewrite of the column names only, not of the rows.
* New statuses get labels in `PRICE_STATUS_LABELS` (`service.py`), the CLI summary (`cli.py`, which today prints "Yahoo Finance only, no
  second source" for every non-HK stock) and the web tooltips, all in English.
* `Market.cross_checks_prices` becomes true for each market that gains a second source; its comment ("only HK, via Tencent") is updated.
  `TencentQuoteProvider` takes the market (prefix, currency) instead of hard-coding `hk` and `HKD`.
* `PARSER_VERSIONS` (`filings.py`) is about filings and does not apply to prices. A change of price source is a data migration of its own
  (section 12).

## 10. Samsung preferred shares (PS and PB basis)

Today (`kifrs.py`, README "Samsung" rows): net profit attributable to owners includes the part attributable to preferred shareholders,
so the weighted average shares used for revenue per share and book value per share are **ordinary plus preferred** shares from the EPS
note, and the price is the ordinary share's (005930.KS). The PE is unaffected: it uses the basic EPS of common stock, which is already per
ordinary share.

Because the preferred shares (005935.KS) trade at a discount, PS and PB as computed equal (ordinary price x all shares) / revenue (or
equity), which is higher than the market-capitalisation basis. With the report's February 2026 figures (ordinary listed shares
5,919,637,922, preferred 815,974,664, prices about 180,900 and 125,900; **not verified** against KRX), the difference is a few percent.
The percentile moves much less, because the discount changes slowly and the whole history is on the same basis.

* **Option A, market-cap basis:** PS = (ordinary price x ordinary shares + preferred price x preferred shares) / revenue TTM; PB the same
  over equity. Needs 005935.KS daily prices (a second price series for one stock, with its own sources and cross-check), a configuration
  key for a second share class, and a new calculation text in `audit.py` and the MCP `calculation`. It changes every historical PS and PB
  value of Samsung (regression, section 12).
* **Option B, keep and say so:** the `calculation` text and a fixed MCP warning state that PS and PB price all shares at the ordinary
  share's price, so they are a few percent above a market-cap basis, and that the percentile is on a consistent basis.

Recommendation (question Q2): **B now, A only if the maintainer wants PS and PB to be comparable with outside sources.** B is a text change
that keeps the history consistent; A doubles the KR price work while the KR source itself is still open. BABA and JD need nothing
(decision 5).

## 11. MCP tool `get_prices`

`get_prices(symbol, start, end, as_of=None)`, read-only and offline like the other tools (`mcp_server.py`, `docs/mcp-server.md`).

* **as_of:** the newest day returned is the last saved trading day on or before `as_of` (default: the newest saved day); `end` after
  `as_of` is cut to `as_of` with a warning, as `get_valuation` does. Only saved, completed days are returned (the same `prices_final`
  rows the valuation used).
* **Result per day** (from `prices_final`, no arithmetic in the model): `day`, `close` (on today's share basis, as used in the
  valuations), `close_as_traded` (the unadjusted close of that day, where a split happened since; from the saved primary close and the
  split factor, computed in Python), `currency`, `source`, `cross_check` (the saved status), `secondary_close` (the other source's
  close on the same basis, if any), `diff_pct` (computed in Python, rounded as the result says).
* **Window:** a maximum number of days per call, like `MAX_HISTORY_POINTS`; longer ranges are refused with the limit in the message.
* **Sources and warnings:** the provider and endpoint of each source, the split records applied, and a warning for every day in the
  range whose status is `mismatch` or single-source, so that the assistant mentions them.
* **Tests:** `tests/test_mcp_server.py` for the rules on synthetic data (as_of cut, limits, statuses, no network, no writes) and
  `tests/test_mcp_production.py` on the real saved data of every configured stock with writes refused and no secrets, as `CLAUDE.md`
  requires. `docs/mcp-server.md` gets the tool and example answers, such as "Samsung closed at 196,900 KRW on 2026-11-19 according to Yahoo
  Finance; it has no second source, so the price is not cross-checked."

It can be built before any source change (it shows today's single-source status honestly) and is useful for checking the backtest.

## 12. Regression and migration

A change of primary source for a market changes historical closes, hence every ratio and every percentile of that market's stocks.
Required before such a change is merged:

1. **Before/after on the same data snapshot:** run the full valuation of every configured stock (all metrics, all lookbacks of
   `lookback_options`) on the current `data/valuation.db` and on a copy with the new prices, offline, and write a report: per stock and
   metric, the current value, percentile and level before and after, and the number of days whose close changed with the largest change.
2. **Every difference explained:** a changed close is either a source correction (the new source matches the reference) or a bug.
   Stocks of markets whose source did not change must show **no** difference at all.
3. **The report goes in the pull request** (and a summary in this document's section 16).
4. **The data migration happens in the scheduled workflow, not locally** (`CLAUDE.md`: only the scheduled workflow commits
   `data/valuation.db`). The code change makes the update job download the new source and rewrite `prices_final` for the affected
   symbols on its next run; the existing `prune=True` and write-only-what-changed rules keep the file growth to the changed rows. A
   switch flag in the meta document of the series (for example `prices/<symbol>/final: {"primary": "naver", "since": ...}`) records which
   source the saved final prices come from, so a rerun is idempotent.
5. The pre-change values stay recoverable from git history (the last workflow commit before the switch).

## 13. How this fits with the trading calendar

[trading-calendar.md](trading-calendar.md) (on hold) uses the saved prices as **evidence** for whether a day traded (its section 8:
`prices_final` is the secondary evidence, an index the primary one), reconciles calendar, index and stocks (its section 9), and plans a
manual repair command that fetches missing days from an alternative source and marks them, for example `patched` (its section 10). It
names the Tencent API as a candidate for A-shares "to be tested" and leaves Korea and the US open.

How the two designs meet:

* **This design supplies the alternative sources** that the calendar's repair command needs: Tencent for CN (and US) is tested here
  (section 6), and the KR source is chosen after the backtest. The calendar does not need to choose sources itself.
* **A second price source makes the calendar's evidence stronger.** Its "common-source risk" (index and stocks all from Yahoo) is
  reduced for every market that gains Tencent; with a second source, "a stock is missing a day" can be told apart from "Yahoo is
  missing a day".
* **Statuses are shared.** `prices_final.status` gets the values of both designs (`agree`, `mismatch`, single-source, and the calendar's
  `patched`); one list of labels in `service.py`.
* **Different questions:** the calendar asks "did the day trade?", this design "is the close right?". Neither replaces the other.

**Order (question Q5).** Recommendation: build this design first, in the order of section 14, up to and including CN on Tencent;
then the trading calendar, whose reconciliation then has two sources for HK and CN. The KR switch waits for the backtest and the
maintainer's decision, whichever module comes first. The MCP `get_prices` tool is independent of both.

## 14. Build plan: pull requests

Each is one pull request, merged before the next, with the full test suite passing on Python 3.10 and 3.12 (pandas 2 and 3) and
`tests/test_english_only.py` green. The version is bumped only when the maintainer releases.

**PR 1: KR backtest (a report, no change to the program's prices).**

* A script outside the run-time package (for example `tools/prices/backtest_kr.py`; it uses the network, like the calendar generator,
  and is not run in CI) that reads the saved Yahoo bars from `data/valuation.db` read-only, fetches the candidate sources, and reads
  the reference file the maintainer downloaded from KRX (section 6.1; kept outside the repository, or committed only if KRX's terms
  allow).
* Period: every trading day since the NXT launch (2025-03-04, **believed**, to be verified), plus a sample before it (at least all of 2024)
  and the days around the 2018-05-04 split.
* Output: per source, the number of days identical to the reference, within 0.1%, 0.2%, 1%, and the largest differences with dates;
  days missing in each source; the result for the two days of the report (2026-02-13, 2026-11-19); by month, to show when deviations
  started (section 5).
* Done when: the report is in the pull request and section 16 of this document; the maintainer decides question Q1.

**PR 2: Tencent for CN, `get_prices`.**

* `TencentQuoteProvider` generalised to markets (`sz` / `sh` prefixes, `CNY`), `cross_checks_prices=True` for `CN`, labels and CLI
  text, schema option of section 9, the README sections on prices and the "Known limitations" bullets.
* The regression of section 12 for the five A-shares (expected: no or near-zero change, as Tencent and Yahoo agreed on the days checked;
  every difference explained), and no change at all for the other stocks.
* `get_prices` (section 11) with its tests and `docs/mcp-server.md`.

**PR 3: A-share share-distribution check and ongoing event check.** Section 7. If a split record is wrong, the fix and its regression
are part of this pull request.

**PR 4: KR source switch (only if decided after PR 1).** The new provider, the cross-check, the regression of section 12 for Samsung,
the README. If the maintainer chose option A of section 10, the preferred-share prices come here too (or in a PR 5).

**PR 5 (optional): US second source** (question Q3), with its regression (US stocks: expected no change).

## 15. Left to the implementer

* The exact Tencent symbol rules for A-shares and the US suffixes (`.OQ`, `.N`, others), and how they are configured (derived from the
  symbol, or a key in `config.yaml`).
* The `prices_final` column option of section 9, unless the maintainer states one.
* The names of the backtest script and of new statuses.
* Whether the Tencent event records are saved (in `meta`) or only used for the check.
* How the regression report is formatted (a Markdown table in the pull request is enough).

## 16. Facts found while designing

Verified (2026-11-25):

* In code: only `HK` has `cross_checks_prices=True`; `TencentQuoteProvider` is HK-only; single-source markets use Yahoo's
  split-adjusted close with factor 1.0 and status `yahoo_only`; `AGREE_TOLERANCE = 0.002`; `MISMATCH_TOLERANCE = 1e-4`; `SCHEMA_VERSION =
  1`; `get_valuation` returns `cross_check` from `prices_final.status`; there is no price-only MCP tool.
* In the saved data: 16 of 18 stocks are `yahoo_only` on every day; the two HK stocks have no `mismatch`. Samsung's 2026-11-19 saved
  close is 196,900 (Yahoo), 2026-02-13 is 181,200. Yahoo has zero-volume rows for Samsung on 2018-04-30 to 2018-05-03 (the trading halt
  around the split), which the zero-volume rule already drops.
* On the web: Tencent `fqkline/get` returns unadjusted daily bars with event records for `sz` / `sh` A-share codes and for US codes with
  an exchange suffix, and no daily bars for `kr005930`; its A-share and US closes equal the saved Yahoo closes on the days checked after
  undoing Yahoo's split adjustment. Naver's daily endpoints give 197,400 for Samsung on 2026-11-19 and differ from the saved Yahoo closes
  on most days since 2026-09 (section 5). Naver's `robots.txt` files disallow all paths.

Not verified (to be checked in the pull request that relies on them):

* The KRX official close of Samsung on 2026-11-19 (the report: 195,600; Naver's endpoint: 197,400; Yahoo: 196,900).
* Whether Yahoo's KR daily bar includes NXT trades, and why its deviations start around September 2026.
* The NXT launch date (2025-03-04) and session hours (until about 20:00), and the current KRX and HKEX tick tables.
* That the KRX Data Marketplace requires a login for downloads, its lockout and password-expiry rules, and its terms for redistribution.
* pykrx's pandas constraint and that its stock prices come from Naver (from the report).
* Naver's terms of use for automated access.
* Baostock's reachability from GitHub runners and Cloud Run; Eastmoney's unreachability from them (from the report).
* Samsung's listed share counts and the ordinary and preferred prices of February 2026 (from the report).
* The report's other sources (GitHub projects and issues) are leads only.

## 17. Risks

* **The reference itself is wrong or unavailable.** Mitigation: the KRX download is the only reference; if it cannot be obtained, the
  backtest reports agreement between candidates only and says it has no reference.
* **An unofficial endpoint changes or disappears** (Tencent, Naver, Yahoo all are). Mitigation: two sources per market, a single-source
  status that is visible, and the freshness check that already fails the run on stale prices.
* **Automated access against a site's terms.** Mitigation: the maintainer decides (question Q1); the design does not pick a source whose
  terms forbid it without that decision.
* **A source switch silently moves percentiles.** Mitigation: the regression of section 12 is required.
* **Schema migration in a file committed to git.** Mitigation: column renames only, no rebuild or vacuum, done by the scheduled workflow.
* **Wider tolerances hide real errors** (section 8).

## 18. Not in scope

* The trading calendar module ([trading-calendar.md](trading-calendar.md)).
* Intraday prices, NXT session prices as a metric, total-return (dividend-adjusted) series.
* A second source for FX rates.
* Third-party financial data (never; the validation report's Naver EPS of 6,564 against the annual report's 6,605 for 2025 confirms the
  rule of using the original filings).
* Changing the HK consolidation.

## 19. Open questions for the maintainer and how to start

**To be answered by the maintainer** (each with a recommendation):

1. **Q1, KR primary source after the backtest.** Recommendation: decide only after PR 1. If Naver matches the KRX reference on all days
   and its use is acceptable to the maintainer despite its `robots.txt`, make it primary and keep Yahoo as the second source; otherwise
   keep Yahoo primary and add Naver (or nothing) only as a cross-check that marks disagreeing days `mismatch`. Not pykrx (decision 4).
2. **Q2, Samsung PS and PB basis.** Recommendation: option B (keep, state it in `calculation` and a warning) now; option A (market-cap
   basis with 005935.KS prices) only if comparability with outside sources matters.
3. **Q3, a second source for the US, and which.** Recommendation: Tencent (unadjusted, with split events, history to at least 2016;
   section 6.4), Yahoo staying primary, in a late optional pull request; skip it if its backtest shows nothing to gain.
4. **Q4, tolerance tiers.** Recommendation: keep 0.2% for `agree`; add a warning tier above 1%; consider "same tick" per market later,
   once the tick tables are verified (section 8).
5. **Q5, build order relative to the trading calendar.** Recommendation: this design's PR 1 to PR 3 first, then the trading calendar;
   the KR switch whenever Q1 is decided (section 13).
6. **Q6, the KRX reference download.** Whether the maintainer can download Samsung's daily closes from the KRX Data Marketplace once by
   hand for PR 1, and whether that file may be committed (KRX terms). Recommendation: download it, keep it out of the repository.
7. **Q7, branch.** As for the trading calendar (its section 22): a long-lived integration branch or pull requests to `main`.
   Recommendation: PR 1 (a report only) can go to `main`; the data-changing pull requests follow the maintainer's release plan.

**How a new session starts (after the answers above):**

1. Read [CLAUDE.md](../CLAUDE.md), then this document in full, then sections 8 to 10 of [trading-calendar.md](trading-calendar.md).
2. Environment: create `.venv` (`python3 -m venv .venv && .venv/bin/pip install -e '.[dev,cloud]'`) and run the full suite once as a
   baseline.
3. Network: as of 2026-11-25 the Tencent quote API, Naver's endpoints and the Yahoo chart API were reachable from the cloud container
   through its proxy. Only the backtest script uses the network; the program's tests stay offline.
4. Start with PR 1. Do not change `data/valuation.db` from a local run.
