"""SAP's quarterly results documents furnished on Form 6-K (IFRS, millions of euros): sap_ifrs.py and the exhibit selection in providers/sec.py."""

import json
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.sap_ifrs import parse_sap_statement

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "data" / "filings" / "SAP"


def table(*rows):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def document(*parts):
    return ("<html><body><p>EXHIBIT 99.1 Exhibit 99.1 Quarterly Statement Q3 2026</p>" + "".join(parts) + "</body></html>").encode()


# the text of the third quarter of 2026, shortened: key facts, the quarter's and the year-to-date income statements, balance sheet, reconciliation
KEY_FACTS = table(
    ["€ millions, unless otherwise stated", "Q1 2025", "Q2 2025", "Q3 2025", "Q4 2025", "TY 2025", "Q1 2026", "Q2 2026", "Q3 2026"],
    ["Total revenue", "9,013", "9,027", "9,076", "9,684", "36,800", "9,555", "9,878", "10,270"],
    ["Earnings per share, basic (IFRS, in €)", "1.52", "1.45", "1.72", "1.58", "6.28", "1.66", "1.89", "1.96"],
    ["Earnings per share, basic (non-IFRS, in €)", "1.44", "1.50", "1.59", "1.62", "6.15", "1.72", "1.59", "1.89"],
)
QUARTER = table(
    ["€ millions, unless otherwise stated", "Q3 2026", "Q3 2025", "∆ in %"],
    ["Cloud", "6,668", "5,290", "26"],
    ["Total revenue", "10,270", "9,076", "13"],
    ["Cost of cloud", "–1,738", "–1,342", "29"],
    ["Profit (loss) after tax", "2,269", "2,051", "11"],
    ["Attributable to owners of parent", "2,249", "2,004", "12"],
    ["Attributable to non-controlling interests", "20", "47", "–58"],
    ["Earnings per share, basic (in €) 1", "1.96", "1.72", "14"],
    ["Earnings per share, diluted (in €) 1", "1.94", "1.71", "14"],
) + (
    "<p>1 For the three months ended September 30, 2026 and 2025, the weighted average number of shares was 1,150 million (diluted 1,157 million)"
    " and 1,164 million (diluted: 1,172 million), respectively (treasury stock excluded).</p>"
)
YEAR_TO_DATE = table(
    ["€ millions, unless otherwise stated", "Q1–Q3 2026", "Q1–Q3 2025", "∆ in %"],
    ["Total revenue", "29,703", "27,116", "10"],
    ["Attributable to owners of parent", "6,360", "5,481", "16"],
    ["Earnings per share, basic (in €) 1", "5.50", "4.70", "17"],
    ["Earnings per share, diluted (in €) 1", "5.48", "4.67", "17"],
) + (
    "<p>1 For the first nine months of 2026 and 2025, the weighted average number of shares was 1,155 million (diluted: 1,161 million)"
    " and 1,166 million (diluted: 1,175 million), respectively (treasury stock excluded).</p>"
)
BALANCE = table(
    ["as at 9/30/2026 and 12/31/2025"],
    ["€ millions", "2026", "2025"],
    ["Treasury shares", "–10,593", "–6,948"],
    ["Equity attributable to owners of parent", "45,948", "44,586"],
    ["Non-controlling interests", "513", "488"],
    ["Total equity", "46,461", "45,073"],
)
RECONCILIATION = table(
    ["€ millions, unless otherwise stated", "Q3 2026", "Q3 2025", "∆ in %"],
    ["IFRS", "Adj.", "Non-IFRS", "Currency Impact", "Non-IFRS Constant Currency", "IFRS", "Adj.", "Non-IFRS", "IFRS", "Non-IFRS", "Non-IFRS Constant Currency"],
    ["Attributable to owners of parent", "2,249", "–72", "2,177", "2,004", "–158", "1,846", "12", "18"],
    ["Earnings per share, basic (in €)", "1.96", "1.89", "1.72", "1.59", "14", "19"],
    ["Earnings per share, basic (in €)", "5.50", "5.20", "4.70", "4.53", "17", "15"],
)


def test_the_quarter_is_the_statement_before_the_three_months_footnote():
    p = parse_sap_statement(document(KEY_FACTS, QUARTER, YEAR_TO_DATE, BALANCE, RECONCILIATION))
    assert (p.period, p.currency, p.quarterly) == (pd.Period("2026Q3"), "EUR", True)
    assert p.values["revenue"] == [10270, 9076, 9878]  # the previous quarter from the key facts
    assert p.values["profit"] == [2249, 2004]
    assert p.values["eps"] == [1.96, 1.72, 1.89]
    assert p.values["non_ifrs_eps"] == [1.89, 1.59, 1.59]  # the reconciliation row whose IFRS figures are those of the quarter
    assert p.values["wa_shares"] == [1150, 1164]
    assert p.values["equity"] == [45948, 44586]


def test_without_key_facts_there_are_two_figures():
    p = parse_sap_statement(document(QUARTER, YEAR_TO_DATE, BALANCE, RECONCILIATION))
    assert p.values["revenue"] == [10270, 9076] and p.values["eps"] == [1.96, 1.72] and p.values["non_ifrs_eps"] == [1.89, 1.59]


def test_footnote_marks_note_numbers_split_initials_and_spaced_minus_signs_are_read():
    quarter = table(  # 2014-2015: "(9 )" note numbers and "*" footnotes; 2019: "T otal revenue"; 2015Q1: "- 23"
        ["€ millions, unless otherwise stated", "Note", "2015", "2014", "Change in %"],
        ["T otal revenue", "4,497", "3,698", "22"],
        ["Total operating expenses", "- 3,859", "- 2,975", "30"],
        ["Profit after tax", "413", "534", "- 23"],
        ["attributable to owners of parent", "414", "534", "-23"],
        ["E arnings per share, basic (in € )*", "(9 )", "0.35", "0.45", "- 23"],
        ["Earnings per share, diluted (in €)*", "(9 )", "0.35", "0.45", "- 23"],
    ) + "<p>* For the three months ended March 31, 2015 and 2014, the weighted average number of shares was 1,195 million (diluted 1,197 million) and 1,194 million (diluted: 1,196 million), respectively.</p>"
    balance = table(["Equity attributable to owners of parent", "22,209", "19,504"], ["Non-controlling interests", "93", "94"])
    reconciliation = table(["Earnings per share, basic (in €)", "0.35", "0.58", "0.45", "0.56", "–23", "5"])
    p = parse_sap_statement(document(quarter, balance, reconciliation))
    assert p.period == pd.Period("2015Q1")
    assert (p.values["revenue"], p.values["profit"], p.values["eps"], p.values["non_ifrs_eps"]) == ([4497, 3698], [414, 534], [0.35, 0.45], [0.58, 0.56])


def test_with_discontinued_operations_the_total_profit_and_eps_and_the_continuing_non_ifrs_eps_are_taken():
    """2023: Qualtrics was sold (discontinued operations). IFRS profit and EPS include it (the last owners' row, the EPS row without "from
    continuing operations"); the non-IFRS EPS is that of the continuing operations, which leaves out the gain on the sale."""
    quarter = table(
        ["€ millions, unless otherwise stated", "Q2 2023", "Q2 2022", "∆ in %"],
        ["Total revenue", "7,554", "7,207", "5"],
        ["Profit (loss) after tax from continuing operations", "724", "613", "18"],
        ["Attributable to owners of parent", "728", "634", "15"],
        ["Profit (loss) after tax from discontinued operations", "2,656", "–409", "<-100"],
        ["Profit (loss) after tax 2", "3,381", "203", ">100"],
        ["Attributable to owners of parent 2", "3,455", "334", ">100"],
        ["Attributable to non-controlling interests 2", "–74", "–131", "–44"],
        ["Earnings per share, basic (in €) 1 from continuing operations", "0.62", "0.54", "15"],
        ["Earnings per share, basic (in €) 1, 2", "2.96", "0.29", ">100"],
        ["Earnings per share, diluted (in €) 1 from continuing operations", "0.62", "0.54", "14"],
    ) + "<p>1 For the three months ended June 30, 2023 and 2022, the weighted average number of shares was 1,169 million (diluted 1,180 million) and 1,171 million (diluted: 1,174 million), respectively.</p>"
    balance = table(["Equity attributable to owners of parent", "41,946", "40,186"], ["Non-controlling interests", "283", "2,662"])
    reconciliation = table(
        ["Earnings per share, basic (in €) from continuing operations", "0.62", "1.07", "0.54", "0.95", "15", "12"],
        ["Earnings per share, basic (in €) 3", "2.96", "3.14", "0.29", "0.96", ">100", ">100"],
    )
    p = parse_sap_statement(document(quarter, balance, reconciliation))
    assert (p.values["profit"], p.values["eps"], p.values["non_ifrs_eps"]) == ([3455, 334], [2.96, 0.29], [1.07, 0.95])


def test_missing_or_disagreeing_figures_are_errors():
    with pytest.raises(ValueError, match="footnote"):
        parse_sap_statement(document(YEAR_TO_DATE, BALANCE, RECONCILIATION))  # a half-year report: six months only
    with pytest.raises(ValueError, match="balance sheet"):
        parse_sap_statement(document(QUARTER, RECONCILIATION))
    with pytest.raises(ValueError, match="non-IFRS"):
        parse_sap_statement(document(QUARTER, BALANCE))
    with pytest.raises(ValueError, match="key facts"):
        parse_sap_statement(document(KEY_FACTS.replace("10,270", "10,271"), QUARTER, BALANCE, RECONCILIATION))
    with pytest.raises(ValueError, match="disagree"):
        parse_sap_statement(document(QUARTER, BALANCE, BALANCE.replace("45,948", "45,949"), RECONCILIATION))
    with pytest.raises(ValueError, match="basic EPS"):
        parse_sap_statement(document(QUARTER.replace("Earnings per share, basic", "Earnings per share"), BALANCE, RECONCILIATION))


def test_the_exhibit_is_known_by_its_statements_not_by_a_headline():
    from valuation_monitor.providers.sec import sap_statement_title

    assert sap_statement_title(document(QUARTER, BALANCE, RECONCILIATION)) == "IFRS results for the three months ended September 30, 2026"
    assert sap_statement_title(document(YEAR_TO_DATE, BALANCE)) is None  # the half-year report furnished next to the second-quarter statement
    assert sap_statement_title(document("<p>SAP Announces Preliminary First Quarter 2016 Results ... IFRS EPS up 37%</p>")) is None
    images = "<p>Primary Financial Statements (A.1) Consolidated Income Statements – Quarter 15/35 " + QUARTER.split("<p>")[1] + '<img src="page15.jpg">'
    assert sap_statement_title(document(images)) is None  # the fourth quarter of 2021: the tables are images


def test_the_config_takes_the_sap_layout_for_us_stocks_only():
    from valuation_monitor.config import load_config, parse_config

    stock = {"symbol": "SAP", "name": "SAP (ADR)", "market": "US", "cik": 1000184, "sec_exhibit": "sap_ifrs", "filing_format": "sap_ifrs", "metrics": ["pe", "pe_non_ifrs"]}
    parsed = parse_config({"stocks": [stock]}).stocks[0]
    assert (parsed.filing_format, parsed.sec_exhibit, parsed.sec_form, parsed.eps_basis) == ("sap_ifrs", "sap_ifrs", "6-K", {})
    with pytest.raises(ValueError, match="does not match market"):
        parse_config({"stocks": [{**stock, "symbol": "0001.HK", "market": "HK"}]})
    names = [s.name for s in load_config(ROOT / "config.yaml").stocks]
    assert names.index("SAP (ADR)") + 1 == names.index("ASML Holding (NY registry shares)")  # just before ASML on the home page


# ---- the saved documents (data/filings/SAP, written by the scheduled update) -------------------------------------------------------------

saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved SAP documents (the scheduled update saves them)")


def saved_statements():
    index = [e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if e["results"]]
    return [(e, parse_sap_statement((FOLDER / e["file"]).read_bytes())) for e in index]


@saved
def test_every_saved_document_parses_and_only_the_image_quarters_are_missing():
    parsed = saved_statements()
    periods = sorted({p.period for _, p in parsed})
    expected = [p for p in pd.period_range("2014Q4", periods[-1], freq="Q") if p not in (pd.Period("2020Q1"), pd.Period("2021Q4"))]
    assert periods == expected and periods[-1] >= pd.Period("2026Q3")
    by_period = {}
    for _, p in parsed:
        by_period.setdefault(p.period, p)
    # the image quarters from the key facts of the next quarter
    assert [by_period[pd.Period("2020Q2")].values[k][2] for k in ("revenue", "eps", "non_ifrs_eps")] == [6521, 0.68, 0.85]
    assert [by_period[pd.Period("2022Q1")].values[k][2] for k in ("revenue", "eps", "non_ifrs_eps")] == [7981, 1.23, 1.85]


@saved
def test_net_profit_over_eps_matches_the_disclosed_shares():
    """Within the rounding of EPS to the cent in every quarter: at most 0.7% when the EPS is EUR 0.50 or more, 1.7% below (2022Q2: 334 / 0.29
    is 1,152 million against 1,171 million, because 334 / 1,171 = 0.285 is rounded to 0.29). The exception is 2019Q1, a small loss:
    -114 / -0.10 is 1,140 million against 1,194 million, because -114 / 1,194 = -0.0955 is rounded to -0.10."""
    for _, p in saved_statements():
        for period, profit, eps, shares in zip([p.period, p.period - 4], p.values["profit"], p.values["eps"], p.values["wa_shares"]):
            deviation = profit / eps / shares - 1
            limit = 0.05 if period == pd.Period("2019Q1") else 0.017 if abs(eps) < 0.5 else 0.007
            assert abs(deviation) <= limit, (period, deviation)


# Where the prior-year column a year later differs from a quarter's own figures: SAP's fourth-quarter statement is preliminary and unaudited,
# and the audited annual report changes a few figures slightly (profit by EUR 1-36 million, EPS by a cent); in 2023 the 2022 revenue (and the
# 2022Q4 profit) were restated for the sale of Qualtrics (continuing operations); in 2024 the non-IFRS figures of 2023 for the new non-IFRS
# definition (share-based payment no longer excluded); and the 2023Q2 profit, whose gain on the Qualtrics sale was restated in the annual report.
RESTATED = {
    "2014Q4": {"profit", "eps"},
    "2015Q4": {"revenue", "profit"},
    "2016Q4": {"profit", "eps", "non_ifrs_eps"},
    "2017Q4": {"profit", "eps"},
    "2018Q2": {"profit"},
    "2018Q3": {"profit", "eps"},
    "2018Q4": {"profit"},
    "2019Q4": {"revenue", "profit", "eps"},
    "2022Q1": {"revenue"},
    "2022Q2": {"revenue", "non_ifrs_eps"},
    "2022Q3": {"revenue", "non_ifrs_eps"},
    "2022Q4": {"revenue", "profit", "eps", "non_ifrs_eps"},
    "2023Q1": {"non_ifrs_eps"},
    "2023Q2": {"profit", "eps", "non_ifrs_eps"},
    "2023Q3": {"non_ifrs_eps"},
    "2023Q4": {"profit", "eps", "non_ifrs_eps"},
}


@saved
def test_the_prior_year_column_agrees_with_the_quarter_a_year_earlier_except_for_restatements():
    by_period = {}
    for _, p in saved_statements():
        by_period.setdefault(p.period, p)
    differences = {}
    for period, p in by_period.items():
        later = by_period.get(period + 4)
        if later is None:
            continue
        for key in ("revenue", "profit", "eps", "non_ifrs_eps", "wa_shares"):
            if later.values[key][1] != p.values[key][0]:
                differences.setdefault(str(period), set()).add(key)
    assert differences == RESTATED
    assert by_period[pd.Period("2023Q1")].values["non_ifrs_eps"][0] == 1.08 and by_period[pd.Period("2024Q1")].values["non_ifrs_eps"][1] == 0.75
