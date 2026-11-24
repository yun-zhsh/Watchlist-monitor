"""Condensed US GAAP statements in HTML: the press releases of Meta Platforms and Cognizant (8-K exhibit 99.1), of Sea Limited (6-K exhibit 99.1)
and ASML's summary statements (6-K, in euros)."""

import json
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.us_release import parse_us_release

ROOT = Path(__file__).resolve().parent.parent
FILINGS = ROOT / "data" / "filings"


def table(*rows):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


def release(income, balance, unit="(In millions, except per share amounts)"):
    return f"<html><body><p>EX-99.1</p><p>{unit}</p>{income}<p>{unit}</p>{balance}</body></html>".encode()


META_INCOME = table(
    ["CONDENSED CONSOLIDATED STATEMENTS OF INCOME"],
    ["(In millions, except per share amounts)"],
    ["Three Months Ended September 30,", "Nine Months Ended September 30,"],
    ["2026", "2025", "2026", "2025"],
    ["Revenue", "64,601", "51,242", "181,712", "141,073"],
    ["Net income", "9,860", "2,709", "52,481", "37,690"],
    ["Earnings per share:"],
    ["Basic", "3.86", "1.08", "20.64", "14.96"],
    ["Diluted", "3.83", "1.05", "20.44", "14.62"],
    ["Weighted-average shares used to compute earnings per share:"],
    ["Basic", "2,552", "2,517", "2,543", "2,520"],
    ["Diluted", "2,573", "2,572", "2,568", "2,578"],
)
META_BALANCE = table(
    ["CONDENSED CONSOLIDATED BALANCE SHEETS"],
    ["September 30, 2026", "December 31, 2025"],
    ["Total assets", "478,836", "366,021"],
    ["Total stockholders' equity", "272,386", "217,243"],
)


def test_meta_layout_in_millions():
    p = parse_us_release(release(META_INCOME, META_BALANCE))
    assert p.period == pd.Period("2026Q3") and p.currency == "USD" and p.quarterly
    assert p.values == {"revenue": [64601.0, 51242.0], "profit": [9860.0, 2709.0], "eps": [3.86, 1.08], "wa_shares": [2552.0, 2517.0], "equity": [272386.0, 217243.0]}


def test_older_cognizant_layout_in_thousands_with_the_date_in_a_second_row_and_a_loss():
    income = table(
        ["Three Months Ended", "Nine Months Ended"],
        ["September 30,", "September 30,"],
        ["2014", "2013", "2014", "2013"],
        ["Revenues", "2,581,009", "2,305,723", "7,520,451", "6,487,701"],
        ["Net income (loss)", "(18,000", "416,000", "1,504,000", "1,553,000"],
        ["Basic earnings (losses) per share", "(0.03", "0.69", "2.54", "2.56"],
        ["Weighted average number of common shares outstanding - Basic", "605,000", "607,000", "593,000", "607,000"],
    )
    balance = table(["September 30,", "December 31,"], ["2014", "2013"], ["Total Liabilities and Stockholders' Equity", "9,227,043", "8,134,718"], ["Stockholders' Equity", "7,381,710", "6,135,791"])
    p = parse_us_release(release(income, balance, "(in thousands, except per share data)"))
    assert p.period == pd.Period("2014Q3")
    assert p.values["revenue"] == pytest.approx([2581.009, 2305.723]) and p.values["profit"] == pytest.approx([-18.0, 416.0])
    assert p.values["eps"] == [-0.03, 0.69] and p.values["wa_shares"] == pytest.approx([605.0, 607.0])  # shares in thousands become millions
    assert p.values["equity"] == pytest.approx([7381.71, 6135.791])


def test_footnote_marks_in_headers_are_ignored():
    income = META_INCOME.replace("<td>2026</td>", "<td>2026 (a)</td>", 1).replace("<td>2025</td>", "<td>2025*</td>", 1)
    balance = META_BALANCE.replace("September 30, 2026", "September 30, 2026 (1)")
    assert parse_us_release(release(income, balance)).period == pd.Period("2026Q3")


def test_a_fourth_quarter_release_takes_the_three_month_group_not_the_year():
    income = META_INCOME.replace("Nine Months Ended September 30,", "Year Ended December 31,").replace("Three Months Ended September 30,", "Three Months Ended December 31,")
    balance = META_BALANCE.replace("September 30, 2026", "December 31, 2026").replace("December 31, 2025", "December 31, 2025")
    p = parse_us_release(release(income, balance))
    assert p.period == pd.Period("2026Q4") and p.values["revenue"] == [64601.0, 51242.0]


def test_missing_data_is_an_error_not_a_guess():
    with pytest.raises(ValueError, match="income statement"):
        parse_us_release(release(table(["Revenue", "1"]), META_BALANCE))
    with pytest.raises(ValueError, match="balance sheet"):
        parse_us_release(release(META_INCOME, table(["Total assets", "1", "2"])))
    with pytest.raises(ValueError, match="balance sheet is for"):
        parse_us_release(release(META_INCOME, META_BALANCE.replace("September 30, 2026", "June 30, 2026")))


def saved(symbol):
    index = [e for e in json.loads((FILINGS / symbol / "index.json").read_text(encoding="utf-8")) if e["results"]]
    return {pd.Timestamp(e["published"]): parse_us_release((FILINGS / symbol / e["file"]).read_bytes()) for e in index}


@pytest.mark.parametrize("symbol, restated", [("META", {"2016Q1", "2016Q2", "2016Q3"}), ("CTSH", set()), ("ASML", set())])
def test_every_saved_release_parses_and_agrees_with_the_year_after(symbol, restated):
    """Each quarter appears in its own release and as the prior-year figure of the release four quarters later: they must agree.

    Meta restated its 2016 quarters for a new accounting rule (ASU 2016-09, adopted in the fourth quarter of 2016), so those three differ.
    """
    parsed = saved(symbol)
    periods = sorted(p.period for p in parsed.values())
    assert periods == list(pd.period_range(periods[0], periods[-1], freq="Q"))  # no quarter missing
    assert periods[0] <= pd.Period("2014Q4") and periods[-1] >= pd.Period("2026Q3")
    by_period = {p.period: p for p in parsed.values()}
    differing = set()
    for p in parsed.values():
        later = by_period.get(p.period + 4)
        if later is None:
            continue
        for key in ("revenue", "profit", "eps"):
            tolerance = 0.006 if key == "eps" else max(1.0, abs(p.values[key][0]) * 0.001)  # amounts: Cognizant moved from thousands to rounded millions in 2016
            if abs(p.values[key][0] - later.values[key][1]) > tolerance:
                differing.add(str(p.period))
    assert differing == restated


def test_cognizants_2014_figures_are_in_millions_like_the_rest():
    parsed = saved("CTSH")
    first = min(parsed.values(), key=lambda p: p.period)
    assert 1_000 < first.values["revenue"][0] < 10_000 and 100 < first.values["wa_shares"][0] < 1_000  # thousands were converted


# ---- ASML: summary US GAAP statements, in euros ----------------------------------------------------------------------------------------

ASML_INCOME = table(
    ["Three months ended,", "Six months ended,"],
    ["Jul 4,", "Jul 3,", "Jul 4,", "Jul 3,"],
    ["(unaudited, in millions €, except per share data)", "2021", "2022", "2021", "2022"],
    ["Total net sales", "4,020.2", "5,430.5", "8,384.1", "8,964.9"],
    ["Net income", "1,038.2", "1,410.9", "2,369.6", "2,106.2"],
    ["Basic net income per ordinary share", "2.52", "3.54", "5.73", "5.29"],
    ["Diluted net income per ordinary share 4", "2.52", "3.54", "5.72", "5.28"],
    ["Weighted average number of ordinary shares used in computing per share amounts (in millions):"],
    ["Basic", "411.5", "398.3", "413.4", "398.4"],
)
ASML_QUARTERLY = table(
    ["Three months ended,"],
    ["Jul 4,", "Oct 3,", "Dec 31,", "Apr 3,", "Jul 3,"],
    ["(unaudited, in millions €, except per share data)", "2021", "2021", "2021", "2022", "2022"],
    ["Total net sales", "4,020.2", "5,241.3", "4,985.6", "3,534.4", "5,430.5"],
    ["Net income", "1,038.2", "1,740.2", "1,773.4", "695.3", "1,410.9"],
    ["Basic net income per ordinary share", "2.52", "4.27", "4.39", "1.73", "3.54"],
    ["Weighted average number of ordinary shares used in computing per share amounts (in millions):"],
    ["Basic", "411.5", "407.9", "404.3", "401.1", "398.3"],
)
ASML_BALANCE = table(
    ["Dec 31,", "Jul 3,"],
    ["(unaudited, in millions €)", "2021", "2022"],
    ["Total assets", "30,231.0", "31,912.6"],
    ["Total shareholders' equity", "10,140.6", "7,648.6"],
)


def test_asml_layout_fiscal_quarter_ends_euros_and_the_year_end_listed_first():
    p = parse_us_release(release(ASML_INCOME, ASML_BALANCE, "(unaudited, in millions €)"))
    assert p.currency == "EUR" and p.period == pd.Period("2022Q2")  # July 3, 2022 is the end of the second quarter
    assert p.values["revenue"] == [5430.5, 4020.2] and p.values["eps"] == [3.54, 2.52] and p.values["wa_shares"] == [398.3, 411.5]
    assert p.values["equity"] == [7648.6, 10140.6]  # this quarter's end, then the year end, although the table lists the year end first


def test_the_quarterly_summary_adds_the_previous_quarter_and_must_agree_with_the_first_table():
    p = parse_us_release(release(ASML_INCOME + ASML_QUARTERLY, ASML_BALANCE, "(unaudited, in millions €)"))
    assert p.values["revenue"] == [5430.5, 4020.2, 3534.4] and p.values["eps"] == [3.54, 2.52, 1.73]  # this quarter, last year's, the previous quarter
    with pytest.raises(ValueError, match="disagree"):
        parse_us_release(release(ASML_INCOME + ASML_QUARTERLY.replace("5,430.5", "5,999.9"), ASML_BALANCE, "(unaudited, in millions €)"))


def test_a_footnote_number_after_the_year_is_ignored():
    income = ASML_INCOME.replace("<td>2021</td>", "<td>2021 3</td>", 1)
    assert parse_us_release(release(income, ASML_BALANCE, "(unaudited, in millions €)")).period == pd.Period("2022Q2")


def test_asml_newest_statements_are_text_without_tables():
    text = (
        "<html><body><div>Contents Net system sales Consolidated Balance Sheets</div>"  # the table of contents does not have the numbers
        "<div>Three months ended Six months ended Jun 30, Jun 29, Jun 30, Jun 29, (unaudited, in millions €, except per share data) 2024 2025 2024 2025 "
        "Net system sales 4,760.9 5,596.1 8,726.8 11,336.5 Total net sales 6,242.8 7,691.7 11,532.8 15,433.2 Net income 1,577.9 2,290.3 2,801.7 4,645.3 "
        "Basic net income per ordinary share 4.01 5.90 7.12 11.90 Diluted net income per ordinary share 4.01 5.90 7.12 11.90 "
        "Weighted average number of ordinary shares used in computing per share amounts (in millions): Basic 393.2 388.2 393.3 390.2 Diluted 393.5 388.4 393.6 390.4</div>"
        "<div>Total shareholders\u2019 equity 18,476.8 17,618.2 Total liabilities and shareholders\u2019 equity 48,589.6 44,000.0</div></body></html>"
    ).encode()
    p = parse_us_release(text)
    assert p.currency == "EUR" and p.period == pd.Period("2025Q2")
    assert p.values == {"revenue": [7691.7, 6242.8], "profit": [2290.3, 1577.9], "eps": [5.9, 4.01], "wa_shares": [388.2, 393.2], "equity": [17618.2, 18476.8]}
    first_quarter = text.replace(b"Three months ended Six months ended Jun 30, Jun 29, Jun 30, Jun 29,", b"Three months ended Mar 30, Mar 29,").replace(b" 2024 2025 2024 2025 ", b" 2024 2025 ")
    assert parse_us_release(first_quarter.replace(b"4,760.9 5,596.1 8,726.8 11,336.5", b"4,760.9 5,596.1").replace(b"6,242.8 7,691.7 11,532.8 15,433.2", b"6,242.8 7,691.7")
                            .replace(b"1,577.9 2,290.3 2,801.7 4,645.3", b"1,577.9 2,290.3").replace(b"4.01 5.90 7.12 11.90", b"4.01 5.90").replace(b"393.2 388.2 393.3 390.2", b"393.2 388.2")).period == pd.Period("2025Q1")


def test_net_income_attributable_to_common_stockholders_is_not_taken_without_non_controlling_interests():
    """Meta 2014-2018: "Net income attributable to Class A and Class B common stockholders" is net income less the participating securities."""
    income = META_INCOME.replace(
        "<tr><td>Earnings per share:</td></tr>",
        "<tr><td>Less: Net income attributable to participating securities</td><td>5</td><td>3</td><td>20</td><td>15</td></tr>"
        "<tr><td>Net income attributable to Class A and Class B common stockholders</td><td>9,855</td><td>2,706</td><td>52,461</td><td>37,675</td></tr>"
        "<tr><td>Earnings per share:</td></tr>",
    )
    assert parse_us_release(release(income, META_BALANCE)).values["profit"] == [9860.0, 2709.0]


# ---- Sea Limited: 6-K press releases, thousands of US dollars, non-controlling interests ---------------------------------------------

SEA_UNIT = "UNAUDITED INTERIM CONDENSED CONSOLIDATED STATEMENT OF OPERATIONS Amounts expressed in thousands of US dollars (\u201c$\u201d) except for number of shares &amp; per share data"
SEA_NINE_MONTHS = table(
    ["For the Nine Months ended September 30,"],
    ["2025", "2026"],
    ["$", "$"],
    ["Revenue"],
    ["Total revenue", "16,086,601", "23,467,552"],
    ["Net income", "1,200,017", "1,398,796"],
    ["Net income attributable to non-controlling interests", "(18,967", "(39,280"],
    ["Net income attributable to Sea Limited\u2019s ordinary shareholders", "1,181,050", "1,359,516"],
    ["Earnings per share:"],
    ["Basic", "1.99", "2.23"],
    ["Diluted", "1.89", "2.15"],
    ["Weighted average shares used in earnings per share computation (in thousands):"],
    ["Basic", "592,592", "610,532"],
    ["Diluted", "638,092", "633,066"],
)
SEA_THREE_MONTH_SUMMARY = table(  # the summary of a third-quarter release: no net income attributable to Sea and no shares
    ["For the Three Months ended September 30,"],
    ["2025", "2026", "YOY%"],
    ["Revenue"],
    ["5,986,024", "8,582,284", "43.4"],
    ["Net income", "374,988", "502,458", "34.0"],
    ["Earnings per share attributable to Sea Limited\u2019s ordinary shareholders:"],
    ["Basic", "0.63", "0.81", "28.6"],
)
SEA_BALANCE = table(
    ["As of December 31,", "As of September 30,"],
    ["2025", "2026"],
    ["Shareholders\u2019 equity"],
    ["Total Sea Limited shareholders\u2019 equity", "12,526,503", "13,303,994"],
    ["Non-controlling interests", "121,825", "150,157"],
    ["Total shareholders\u2019 equity", "12,648,328", "13,454,151"],
    ["Total liabilities and shareholders\u2019 equity", "29,370,979", "33,201,286"],
)


def test_sea_third_quarter_has_year_to_date_statements_only():
    p = parse_us_release(release(SEA_THREE_MONTH_SUMMARY + SEA_NINE_MONTHS, SEA_BALANCE, SEA_UNIT))
    assert p.period == pd.Period("2026Q3") and p.currency == "USD" and not p.quarterly  # nine months: the quarter is derived by subtraction
    assert p.values["revenue"] == pytest.approx([23467.552, 16086.601]) and p.values["eps"] == [2.23, 1.99]
    assert p.values["profit"] == pytest.approx([1359.516, 1181.05])  # attributable to Sea's shareholders, not the net income with non-controlling interests
    assert p.values["wa_shares"] == pytest.approx([610.532, 592.592])  # "(in thousands)" in the label wins over the table note
    assert p.values["equity"] == pytest.approx([13303.994, 12526.503])  # Sea's shareholders, not the total with non-controlling interests


def test_sea_first_quarter_three_month_statements_with_single_shares_and_a_loss():
    income = table(
        ["For the Three Months ended March 31,"],
        ["2019", "2020"],
        ["$", "$"],
        ["Total revenue", "351,866", "714,920"],
        ["Net loss", "(689,609", "(280,791"],
        ["Net profit attributable to non-controlling interests", "(746", "(722"],
        ["Net loss attributable to Sea Limited\u2019s ordinary shareholders", "(690,355", "(281,513"],
        ["Loss per share:"],
        ["Basic and diluted", "(1.86", "(0.61"],
        ["Weighted average shares used in loss per share computation:"],
        ["Basic and diluted", "370,724,164", "462,194,052"],
    )
    balance = table(
        ["As of December 31, 2019", "As of March 31, 2020"],
        ["Total Sea Limited shareholders\u2019 (deficit) equity", "1,162,424", "920,601"],
        ["Non-controlling interests", "10,228", "46,374"],
        ["Total shareholders\u2019 (deficit) equity", "1,172,652", "966,975"],
    )
    p = parse_us_release(release(income, balance, SEA_UNIT))
    assert p.period == pd.Period("2020Q1") and p.quarterly
    assert p.values["profit"] == pytest.approx([-281.513, -690.355]) and p.values["eps"] == [-0.61, -1.86]
    assert p.values["wa_shares"] == pytest.approx([462.194052, 370.724164])  # single shares ("except for number of shares") -> millions
    assert p.values["equity"] == pytest.approx([920.601, 1162.424])


def test_sea_2017_note_column_and_shares_on_the_heading_row():
    income = table(
        ["For the Three Months ended December 31,", "For the Year ended December 31,"],
        ["Note", "2016", "2017", "2016", "2017"],
        ["Total revenue", "2", "88,461", "124,604", "345,670", "414,190"],
        ["Net loss", "(72,236", "(263,140", "(224,955", "(561,166"],
        ["Net (gain) loss attributable to non-controlling interests", "(4", "480", "2,088", "681"],
        ["Net loss attributable to Sea Limited\u2019s ordinary shareholders", "(72,240", "(262,660", "(222,867", "(560,485"],
        ["Adjusted net loss (1)", "(61,981", "(251,563", "(196,114", "(532,530"],
        ["Loss per share:"],
        ["Basic and diluted", "(0.42", "(0.90", "(1.30", "(2.72"],
        ["Weighted average shares used in loss per share computation:", "171,565,142", "293,324,598", "171,127,788", "205,727,195"],
        ["Basic and diluted"],
    )
    balance = table(
        ["As of December 31,"],
        ["2016", "2017"],
        ["Total Sea Limited shareholders\u2019 deficit", "(125,670", "469,025"],
        ["Non-controlling interests", "20", "6,106"],
        ["Total shareholders\u2019 deficit", "(125,650", "475,131"],
        ["Total liabilities, mezzanine equity and shareholders\u2019 equity", "485,775", "1,988,280"],
    )
    p = parse_us_release(release(income, balance, SEA_UNIT))
    assert p.period == pd.Period("2017Q4") and p.quarterly  # the three-month group, the note number "2" skipped
    assert p.values["revenue"] == pytest.approx([124.604, 88.461]) and p.values["profit"] == pytest.approx([-262.66, -72.24])
    assert p.values["wa_shares"] == pytest.approx([293.324598, 171.565142])
    assert p.values["equity"] == pytest.approx([469.025, -125.67])  # one "As of December 31," over both years


SEA = FILINGS / "SE"
sea_saved = pytest.mark.skipif(not (SEA / "index.json").exists(), reason="no saved Sea filings (the scheduled update saves them)")


@sea_saved
def test_every_saved_sea_release_parses():
    """Sea: three-month statements in the first and fourth quarters (and 2017Q3), year-to-date ones in the second and third quarters from 2018 on."""
    parsed = saved("SE")
    by_period = {p.period: p for p in parsed.values()}
    periods = sorted(by_period)
    assert periods[0] == pd.Period("2017Q3") and periods[-1] >= pd.Period("2026Q3")
    assert periods == list(pd.period_range(periods[0], periods[-1], freq="Q"))
    for period, p in by_period.items():
        assert p.currency == "USD"
        assert p.quarterly == (period.quarter in (1, 4) or period == pd.Period("2017Q3")), period
        profit, eps, shares = p.values["profit"][0], p.values["eps"][0], p.values["wa_shares"][0]
        assert abs(profit / shares - eps) <= 0.005 + 1e-9, period  # basic EPS = net profit / shares, rounded to the cent
    for period, p in by_period.items():  # the prior-year column of the release a year later, on the same basis
        later = by_period.get(period + 4)
        if later is None or later.quarterly != p.quarterly:
            continue
        for key in ("revenue", "profit", "eps", "wa_shares"):
            assert later.values[key][1] == pytest.approx(p.values[key][0], abs=0.006 if key == "eps" else 1.0), (period, key)


@sea_saved
def test_sea_known_values():
    by_period = {p.period: p for p in saved("SE").values()}
    q = by_period[pd.Period("2026Q3")]
    assert q.values["revenue"] == pytest.approx([23467.552, 16086.601]) and q.values["equity"] == pytest.approx([13303.994, 12526.503])
    q = by_period[pd.Period("2018Q4")]
    assert q.values["equity"][0] == pytest.approx(-243.139)  # negative equity at the end of 2018: PB is blank
