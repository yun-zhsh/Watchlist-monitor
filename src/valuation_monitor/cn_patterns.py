"""Chinese-language patterns for reading Chinese filings.

CNINFO announcement titles and the text of Chinese periodic reports are in Chinese, so the patterns that
recognise them have to be too. This is the only module that contains Chinese, and none of it is ever displayed:
everything the user sees (titles, messages, column names) is English.
"""

from __future__ import annotations

import re

# --- CNINFO announcement titles -------------------------------------------------------------------------
# Shanghai-listed companies put their full company name (Bank of Nanjing from 2021 on) or their short name (China Northern
# Rare Earth from 2021 on) before the title. Some annual report titles drop the second "year" character (China Northern Rare
# Earth's 2019 annual report).
COMPANY_PREFIX = r"(?:[一-鿿]+股份有限公司|[一-鿿A-Za-z*]{2,8})?"
TITLE_RE = re.compile(r"^" + COMPANY_PREFIX + r"(?P<year>\d{4})年(?P<kind>第一季度|一季度|半年度|第三季度|三季度|年度|度)报告(?:全文|正文)?(?:（更新后）)?$")
EXPRESS_RE = re.compile(r"^" + COMPANY_PREFIX + r"(?P<year>\d{4})年(?P<kind>半年度|度)业绩快报(?:公告)?$")  # Shanghai: "... announcement"
QUARTER_OF = {"第一季度": 1, "一季度": 1, "半年度": 2, "第三季度": 3, "三季度": 3, "年度": 4, "度": 4}
# Titles containing any of these are summaries, English translations, ESG reports, corrections and the like.
EXCLUDED_TITLE_WORDS = ("摘要", "英文", "ESG", "社会责任", "内部控制", "审计", "环境", "更正", "取消", "补充", "说明", "预告")
EXPRESS_SEARCH_WORD = "业绩快报"
BODY_TEXT_WORD = "正文"  # a report is published twice, as "full text" and as "body text"; the full text is preferred

# --- text of the reports ---------------------------------------------------------------------------------
# A report typeset in Hong Kong fonts (Bank of Nanjing's 2017 annual report) reads as traditional characters once its fonts
# are decoded (cid_fonts.py), and some of its punctuation as look-alike Greek and Latin letters (round brackets, the
# enumeration comma, the colon and the comma); TO_SIMPLIFIED maps those back as well.
# Such a document is detected by traditional words that never occur in simplified text, and the characters of the labels
# the parser looks for are converted to simplified ones; nothing else changes.
TRADITIONAL_RE = re.compile(r"營業收入|歸屬於")
TO_SIMPLIFIED = str.maketrans(
    "會計數據財務標營業歸屬於東淨潤釋權優續債單幣總資產負併損內個為與結餘準備際稅經",
    "会计数据财务标营业归属于东净润释权优续债单币总资产负并损内个为与结余准备际税经",
)
TO_SIMPLIFIED.update(str.maketrans("δεȽφθ", "（）、：，"))
TRADITIONAL_ENUMERATION_COMMA = r"\s*Ƚ\s*"  # the look-alike enumeration comma comes with spaces around it after an item number; they are removed with it
CJK_CHAR = r"[一-鿿]"  # a Chinese character: a page without any has no usable text layer (a scan)
CJK_GAP_RE = r"(?<=[一-鿿（）]) (?=[一-鿿（）])"  # PDF text extraction inserts spaces where lines wrap inside Chinese text
# Annual and half-year statements have a note column after the item name: a Chinese numeral in brackets or followed by a separator, then the note number.
# Shanghai reports may put a "note" word before the note number (note VII.61, or note 40 without the Chinese numeral).
# Banks' audited statements give just the note number ("basic EPS 47 1.76 1.83", "trading assets 7(a) ..."): a one- or
# two-digit integer is a note only when an amount with a thousands separator or decimals follows.
NOTE = (
    r"(?:(?:附注\s*)?(?:[（(][一二三四五六七八九十]+[)）]|[一二三四五六七八九十]+、)\s?\d+(?:、\d+)*\s+|注释\s*\d+\s+"
    r"|\d{1,2}(?:\([a-z]\))?\s+(?=[(-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,])|[(-]?\d+\.\d+))?"
)
FOOTNOTE = r"(?:\[\s*注\s*\d*\s*\])?"  # footnote marker in the key-data table, e.g. "[note 2]"

KEY_TABLE = "主要会计数据和财务指标"
# Shanghai quarterly reports before 2021 (China Northern Rare Earth) call the key-data table just "main financial data"; banks
# (Bank of Nanjing) also use "main accounting data and financial indicators" with another conjunction. Each FALLBACK is
# tried only where the usual name is not found.
KEY_TABLE_FALLBACK = r"主要会计数据(?:和|及)财务指标|主要财务数据"
KEY_REVENUE = r"营业(?:总)?收入（元）"  # older ChiNext reports (Innolight) label the total operating revenue
# In the parser the unit is optional: Shanghai reports state "unit: yuan" (banks: thousand yuan) above the table instead of in
# every row; some bank tables say "(thousand yuan)" in the row name
KEY_UNIT = r"(?:（元）)?"
THOUSAND = r"(?:\s*[（(]\s*千元\s*[)）])?"
# not an amount in the prose: a bank report also gives the revenue in hundred million (or ten thousand) yuan, or a growth rate
NOT_PROSE = r"(?!\s*(?:\[\s*注\s*\d*\s*\])?\s*-?[\d,]+(?:\.\d+)?\s*[亿万%％])"
KEY_REVENUE_ROW = r"营业(?:总)?收入" + KEY_UNIT + THOUSAND + NOT_PROSE
KEY_PROFIT = r"归属于(?:上市)?公司(?:普通股)?股东的净利润" + KEY_UNIT + THOUSAND + NOT_PROSE  # older reports (Innolight) say "the company's ordinary shareholders"
KEY_EPS = r"基本每股收益[（(]元\s*[/／]\s*股[)）]"  # Shanghai reports may use a full-width slash
# Shanghai Q3 reports before 2021 give only year-to-date (January to September) figures in the key-data table: this header
# stands right before the revenue row instead of the "this reporting period" (single quarter) column of other reports
KEY_YTD_ONLY_HEADER = (
    r"年初至报告期末\s*[（(]\s*1\s*[-－—]\s*9\s*月\s*[)）]\s*上年初至上年报告期末\s*[（(]\s*1\s*[-－—]\s*9\s*月\s*[)）]"
    r"\s*比上年同期增减\s*[（(]\s*%\s*[)）]\s*"
)
KEY_PROFIT_FALLBACK = r"归属于(?:上市公司|母公司)股东的?净利润" + THOUSAND + NOT_PROSE  # some bank tables leave out the particle before "net profit"
KEY_EPS_FALLBACK = r"基本每股收益\s*[（(]\s*(?:人民币)?元\s*(?:[/／╱]\s*股\s*)?[)）]"  # yuan per share with other slashes, or just yuan
INCOME_REVENUE = r"一、营业(?:总)?收入"
# Banks' audited statements do not number the items: the operating revenue is the row before the net interest income
INCOME_REVENUE_FALLBACK = r"营业收入"
INCOME_REVENUE_FALLBACK_NEXT = r"(?=\s*利息净收入)"
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
EQUITY_FALLBACK = r"归属于母公司(?:股东的|股权)权益(?:合计)?"  # banks' reports (a typo in some; 2019 H1 without "total")
INCOME_EPS_FALLBACK = r"基本每股收益(?:及|和)稀释每股收益(?:\s*[（(]\s*人民币元\s*/\s*股\s*[)）])?"  # one row for both (banks, 2014-2017)
SHARE_CAPITAL = r"(?:实收资本（或股本）|(?<!总)股本)"
SHARE_CAPITAL_FALLBACK = r"普通股总股本"  # a bank's key-data table (Bank of Nanjing's 2019 Q1 report)
# Preference shares and perpetual bonds, listed under the other equity instruments of the equity section: they are not
# equity of the ordinary shareholders, so PB leaves them out (banks)
OTHER_EQUITY_INSTRUMENTS = r"其他权益工具(?!投资)"
# Without the two rows (Bank of Nanjing until 2018, when the instruments were all preference shares), the whole row is left
# out, but only in a report that gives the equity of the ordinary shareholders separately
ORDINARY_SHAREHOLDERS_RE = re.compile(r"普通股股东的(?:每股)?(?:净资产|所有者权益)")
PREFERENCE_SHARES = r"优先股"
NO_AMOUNT = r"(?:[-–—－]|不适用)"  # an empty cell: a dash, or "not applicable"
PERPETUAL_BONDS = r"永续债"
# The unit of the amounts in a periodic report: the last unit statement before a row. Yuan, or thousand yuan in bank
# reports: a "unit: RMB thousand" line in its several wordings, a statement page header "RMB thousand", or "(thousand
# yuan)" after a row name. Amounts in the prose (in hundred million yuan) are not unit statements.
UNIT_RE = re.compile(
    r"(单位\s*(?:均为|为)?\s*[：:]?\s*(?:人民币)?\s*千元|人民币千元|千元人民币|[（(]\s*千元\s*[)）])"
    r"|单位\s*(?:均为|为)?\s*[：:]\s*(?:人民币)?\s*元(?![/／╱])"
)
YUAN_LABEL_RE = re.compile(r"[（(]\s*元\s*[)）]")

EXPRESS_REVENUE = r"营业总收入"
EXPRESS_REVENUE_FALLBACK = r"营业收入"  # banks
EXPRESS_PROFIT = r"(?<!后的)归属于上市公司股东的净利润"
EXPRESS_PROFIT_FALLBACK = r"(?<!后的)归属于母公司股东的净利润"
EXPRESS_EPS = r"基本每股收益"
EXPRESS_EPS_UNIT = r"(?:（元(?:\s*/\s*股)?）)?"
EXPRESS_EQUITY = r"归属于上市公司股东的所有者权益"
# Banks also give the equity of the ordinary shareholders (without preference shares and perpetual bonds): used first when present
EXPRESS_ORDINARY_EQUITY = r"归属于(?:上市公司|母公司)普通股股东的(?:所有者权益|净资产)"
EXPRESS_EQUITY_FALLBACK = r"归属于(?:上市公司|母公司)股东的净资产"
EXPRESS_SHARES = r"股本"
EXPRESS_HUNDRED_MILLION_RE = re.compile(r"单位\s*[：:]\s*(?:人民币)?亿元")  # banks' express reports
EXPRESS_HUNDRED_MILLION_SHARES = r"（亿股）"
EXPRESS_EPS_NOTE = r"(?:注\s*[：:])?"  # "basic EPS (yuan) note:" before the numbers
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
    key_table = KEY_TABLE in text or re.search(KEY_REVENUE, text) or re.search(KEY_TABLE_FALLBACK, text)
    # The income statement can run over three pages (Glodon's Q1 reports): the page with the net profit keeps the EPS page after it
    income = re.search(INCOME_REVENUE, text) or re.search(INCOME_PROFIT, text) or re.search(INCOME_REVENUE_FALLBACK + r"\s+[\d,]+\s+[\d,]+" + INCOME_REVENUE_FALLBACK_NEXT, text)
    balance = ("归属于母公司" in text and EQUITY_TOTAL_RE.search(text)) or (re.search(r"(?<!总)股本", text) and "资本公积" in text)
    return bool(key_table or income or balance)
