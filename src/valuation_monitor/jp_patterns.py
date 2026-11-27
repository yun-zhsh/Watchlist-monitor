"""Japanese-language patterns used to read the Kessan Tanshin of Japanese companies (TSE) and the titles of their IR library.

The project is English-only; this module (like cn_patterns.py for the A-share reports) is the exception, because these
patterns *read* the original Japanese filings. Nothing here is ever displayed: the provider gives every filing an English
title and the parser returns numbers only. The text is matched after Unicode NFKC normalization (full-width digits, letters
and brackets become ASCII) and with the spaces between Japanese characters removed (see jp_tanshin.read_text).
"""

from __future__ import annotations

# Japanese characters (kana and CJK ideographs), for removing the spaces the PDF text layer puts between them
JP_CHAR = r"[぀-ヿ㐀-鿿]"
JP_GAP_RE = rf"(?<={JP_CHAR}) (?={JP_CHAR})"

# ---- titles of the IR library (Xj-Storage) ---------------------------------------------------------------------------------
TANSHIN_WORD = "決算短信"  # Kessan Tanshin (summary of financial results)
CORRECTION_WORD = "訂正"  # correction
EXCLUDED_TITLE_WORDS = ("説明", "補足", "英文")  # results presentations, supplementary material, English translations
# The fiscal year and quarter of a title, e.g. the first quarter of the year ending August 2026, the year ending August Heisei 27 (2015),
# the second quarter (interim period) of the year ending August 2025; the fiscal year is named after the month it ends, and a title
# without a quarter is the full year
TITLE_RE = r"(?:平成(?P<heisei>\d{1,2})|(?P<year>\d{4}))年\s?(?P<month>\d{1,2})月期\s?(?:第\s?(?P<quarter>[1-3])\s?四半期)?"
HEISEI_OFFSET = 1988  # Heisei 1 = 1989

# ---- the summary on the first pages of a Kessan Tanshin ---------------------------------------------------------------------
OPERATING_RESULTS = "連結経営成績"  # (1) consolidated operating results: net sales, operating profit, ordinary profit, profit attributable to owners of parent
# The label of a row: the year ending August 2026, first quarter; the interim period (the first half, so labelled since FY2025);
# the full year; or the Heisei year without the era name (27 for 2015, used up to FY2017)
ROW_LABEL = r"(?:平成)?(?P<year>\d{4}|\d{2})年(?P<month>\d{1,2})月期(?:第(?P<quarter>[1-4])四半期|(?P<interim>中間期))?"
EPS_SECTION = "1株当たり"  # the per-share table that follows: basic EPS, diluted EPS (and, for the full year, ROE etc.)
EQUITY_LABEL = r"\(参考\)\s?自己資本"  # (reference) equity: net assets less share acquisition rights and non-controlling interests
MILLION_YEN = "百万円"
ISSUED_SHARES = r"期末発行済株式数\(自己株式を含む\)"  # shares issued at the end of the period, including treasury shares
TREASURY_SHARES = r"期末自己株式数"  # treasury shares at the end of the period (including those held by the ESOP trust)
# The label of a share count row: the year ending August 2026 with "1Q", the interim period, the full year, or a Heisei year
SHARE_ROW_LABEL = r"(?:平成)?(?:\d{4}|\d{2})年\d{1,2}月期(?:\s?\dQ|中間期)?"
SHARES_UNIT = "株"
MINUS_SIGNS = "△▲"  # a negative number is written with a triangle
DASHES = "―—‐-ー"  # no figure (the first year after the listing has no prior-year figures)
