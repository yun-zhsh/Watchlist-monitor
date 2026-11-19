"""Chinese-language patterns for reading Chinese filings.

CNINFO announcement titles and the text of Chinese periodic reports are in Chinese, so the patterns that
recognise them have to be too. This is the only module that contains Chinese, and none of it is ever displayed:
everything the user sees (titles, messages, column names) is English.
"""

from __future__ import annotations

import re

# --- CNINFO announcement titles -------------------------------------------------------------------------
# Shanghai-listed companies (China Northern Rare Earth since 2021) put their short name before the year, and some titles
# drop the second "year" character before "annual report" (2019 annual report); their express reports end in "announcement".
SHORT_NAME_PREFIX = r"(?:[一-鿿A-Za-z*]{2,8})?"
TITLE_RE = re.compile(rf"^{SHORT_NAME_PREFIX}(?P<year>\d{{4}})年(?P<kind>第一季度|一季度|半年度|第三季度|三季度|年度|度)报告(?:全文|正文)?(?:（更新后）)?$")
EXPRESS_RE = re.compile(rf"^{SHORT_NAME_PREFIX}(?P<year>\d{{4}})年(?P<kind>半年度|度)业绩快报(?:公告)?$")
QUARTER_OF = {"第一季度": 1, "一季度": 1, "半年度": 2, "第三季度": 3, "三季度": 3, "年度": 4, "度": 4}
# Titles containing any of these are summaries, English translations, ESG reports, corrections and the like.
EXCLUDED_TITLE_WORDS = ("摘要", "英文", "ESG", "社会责任", "内部控制", "审计", "环境", "更正", "取消", "补充", "说明", "预告")
EXPRESS_SEARCH_WORD = "业绩快报"
BODY_TEXT_WORD = "正文"  # a report is published twice, as "full text" and as "body text"; the full text is preferred

# --- text of the reports ---------------------------------------------------------------------------------
CJK_GAP_RE = r"(?<=[一-鿿（）]) (?=[一-鿿（）])"  # PDF text extraction inserts spaces where lines wrap inside Chinese text
# Annual and half-year statements have a note column after the item name: a Chinese numeral in brackets or followed by a separator, then the note number.
# Shanghai reports may put a "note" word before the note number (note VII.61, or note 40 without the Chinese numeral).
NOTE = r"(?:(?:附注\s*)?(?:[（(][一二三四五六七八九十]+[)）]|[一二三四五六七八九十]+、)\s?\d+(?:、\d+)*\s+|注释\s*\d+\s+)?"
FOOTNOTE = r"(?:\[\s*注\s*\d*\s*\])?"  # footnote marker in the key-data table, e.g. "[note 2]"

KEY_TABLE = "主要会计数据和财务指标"
# Shanghai quarterly reports before 2021 (China Northern Rare Earth) call the key-data table just "main financial data"
KEY_TABLE_FALLBACK = "主要财务数据"
KEY_REVENUE = r"营业(?:总)?收入（元）"  # older ChiNext reports (Innolight) label the total operating revenue
# In the parser the unit is optional: Shanghai reports state "unit: yuan" above the table instead of in every row
KEY_UNIT = r"(?:（元）)?"
KEY_REVENUE_ROW = r"营业(?:总)?收入" + KEY_UNIT
KEY_PROFIT = r"归属于(?:上市)?公司(?:普通股)?股东的净利润" + KEY_UNIT  # older reports (Innolight) say "the company's ordinary shareholders"
KEY_EPS = r"基本每股收益[（(]元\s*[/／]\s*股[)）]"  # Shanghai reports may use a full-width slash
# Shanghai Q3 reports before 2021 give only year-to-date (January to September) figures in the key-data table: this header
# stands right before the revenue row instead of the "this reporting period" (single quarter) column of other reports
KEY_YTD_ONLY_HEADER = (
    r"年初至报告期末\s*[（(]\s*1\s*[-－—]\s*9\s*月\s*[)）]\s*上年初至上年报告期末\s*[（(]\s*1\s*[-－—]\s*9\s*月\s*[)）]"
    r"\s*比上年同期增减\s*[（(]\s*%\s*[)）]\s*"
)
INCOME_REVENUE = r"一、营业(?:总)?收入"
INCOME_PROFIT = r"归属于母公司(?:所有者|股东)的净利润"
# Some companies (Glodon) add the sign convention after the item name ("net loss shown with a minus sign")
# Its last words ("filled in" and the bracket) can be pushed past the numbers by a page break (China Northern Rare Earth's 2026 Q3 report)
LOSS_SIGN_NOTE = r"(?:[（(]\s*净亏损以\s*[“\"]?\s*[-－—]\s*[”\"]?\s*号(?:\s*填列\s*[)）])?)?"
# Companies with a finance business (Glodon) report total operating revenue = operating revenue + interest and fee income;
# the key-data table and the headline revenue are the operating revenue, the first item under the total.
INCOME_OPERATING_REVENUE = r"其中\s*[：:]\s*营业收入"
BEFORE_OPERATING_REVENUE = r"[^、]{0,80}?"  # a page footer may sit between the total and the operating revenue, but no other numbered item
INCOME_EPS = r"基本每股收益(?:\s*[（(]\s*元\s*/\s*股\s*[)）])?"
EQUITY = r"归属于母公司(?:所有者|股东)权益(?:（或股东权益）)?合计"  # Shanghai reports add "(or shareholders' equity)"
SHARE_CAPITAL = r"(?:实收资本（或股本）|(?<!总)股本)"

EXPRESS_REVENUE = r"营业总收入"
EXPRESS_PROFIT = r"(?<!后的)归属于上市公司股东的净利润"
EXPRESS_EPS = r"基本每股收益"
EXPRESS_EPS_UNIT = r"(?:（元(?:\s*/\s*股)?）)?"
EXPRESS_EQUITY = r"归属于上市公司股东的所有者权益"
EXPRESS_SHARES = r"股本"
# Units stated as ten thousand yuan and ten thousand shares (Innolight's 2025 annual express report)
EXPRESS_TEN_THOUSAND_RE = re.compile(r"单位\s*[：:]\s*(?:人民币)?万元")
EXPRESS_TEN_THOUSAND_SHARES = r"（万股）"


# The consolidated balance sheet, or the only one of a company without subsidiaries (Innolight before its 2017 acquisition);
# not the parent-company balance sheet that follows the consolidated one
BALANCE_SHEET_TITLE_RE = re.compile(r"合并资产负债表|(?<!母公司)资产负债表\s*编制单位")
EQUITY_TOTAL_RE = re.compile(r"权益(?:（或股东权益）)?合计")


def is_wanted_page(text: str) -> bool:
    """Is this page of a report one the parser needs (key-data table, income statement, balance sheet)?"""
    # The heading of the key-data table can end a page and the table start the next one (Innolight's half-year reports)
    key_table = KEY_TABLE in text or KEY_TABLE_FALLBACK in text or re.search(KEY_REVENUE, text)
    # The income statement can run over three pages (Glodon's Q1 reports): the page with the net profit keeps the EPS page after it
    income = re.search(INCOME_REVENUE, text) or re.search(INCOME_PROFIT, text)
    balance = ("归属于母公司" in text and EQUITY_TOTAL_RE.search(text)) or (re.search(r"(?<!总)股本", text) and "资本公积" in text)
    return bool(key_table or income or balance)
