# Project rules

## Language (important)

- **The working language of this project is English.** Discussion with the maintainer may be in Chinese, but
  everything that lands in the repository is in English: code, comments, docstrings, tests, README and other docs,
  commit messages, PR titles and descriptions, config, and generated data. A developer other than the maintainer
  who opens the project must see English only.
- **The software is English-only at runtime.** Anyone running it sees English only, with no Chinese anywhere,
  **including stock names** (use English names in `config.yaml`, e.g. Tencent Holdings, Hikvision).
- "Runtime-visible" means: web pages (text, buttons, tooltips, charts), CLI output and error messages,
  downloaded files (file names, CSV column names and contents, zip), generated data files under `data/`
  (CSV/JSON, including filing titles and source notes), GitHub Actions step names, and everything in `config.yaml`.
- Exceptions, because they are Chinese or Japanese by nature: `src/valuation_monitor/cn_patterns.py` (patterns used to *read*
  Chinese-language A-share filings; never displayed), the Chinese string fixtures in `tests/test_cn.py`, and the saved
  original Chinese PDFs under `data/filings/002415.SZ/`, `data/filings/002410.SZ/` and `data/filings/300308.SZ/`;
  likewise `src/valuation_monitor/jp_patterns.py` (patterns used to *read* the Japanese Kessan Tanshin), the Japanese string
  fixtures in `tests/test_jp.py`, and the saved original Japanese PDFs under `data/filings/3697.T/`; likewise the saved original PDFs of SK Hynix
  under `data/filings/000660.KS/` may contain Korean (footnotes of its earnings presentations; the pages saved so far have none), while their
  saved file names, the index and everything read from them stay English.
- `tests/test_english_only.py` enforces this (string literals, comments and docstrings in `src/` and `tests/`,
  templates, static files, config, workflow, README/docs, `data/` text files and the text cells of `data/valuation.db`, rendered pages and downloads).
  Keep it green; extend it when adding new output channels or file types.
- Secrets such as `SEC_USER_AGENT` are provided through environment variables only; never commit them.

## Workflow notes

- All saved data except the original filings lives in the SQLite database `data/valuation.db` (see `db.py`). Write only what changed, never rebuild or vacuum the file, and do not commit it from a local run: only the scheduled workflow does.
- Run the tests with `.venv/bin/pytest` (offline).
- Bump the layout's number in `PARSER_VERSIONS` (`filings.py`) whenever parsing rules or output text of a parser change.
- The MCP server (`mcp_server.py`, docs/mcp-server.md) must stay read-only and offline. A new tool needs an `as_of` date, sources in its result, and no arithmetic outside Python, with tests in `tests/test_mcp_server.py`. Test it on the real saved data of every configured stock with writes refused and no secrets (`tests/test_mcp_production.py`), not only on synthetic data.
- Google Cloud is not deployed automatically. After a pull request is merged into `main`, ask the maintainer whether to deploy it to
  Google Cloud. If yes, give the four steps of "Updating later" in `docs/deploy-google-cloud.md` (region, `scripts/deploy-cloud.sh all`,
  run the update job and check that it succeeded, then `scripts/deploy-cloud.sh mcp`), explained in the language of the conversation. There are no GCP
  credentials here, so the maintainer runs them in Cloud Shell.
- The trading calendar module is specified in `docs/trading-calendar.md` (decisions, data files, rules, the four pull requests, golden dates). It is **on hold**: do not build any part of it unless the maintainer asks. When it is built, build it as written there (sections 21 and 22 first); if something in it turns out wrong, correct the document in the same pull request.
- Price sources (a second source for the KR, CN and US prices, the Korean backtest, `get_prices`) are specified in `docs/price-sources.md` (decisions, current state, the pull requests, open questions). It is **on hold**: do not build any part of it unless the maintainer asks. When it is built, follow it (section 19 first); if something in it turns out wrong, correct the document in the same pull request.
