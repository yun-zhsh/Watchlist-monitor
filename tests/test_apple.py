"""Apple (AAPL): 8-K press releases in the us_press_release layout, with a fiscal year ending on the last Saturday of September, share counts
in thousands under a note that excepts them from the unit ("except number of shares which are reflected in thousands"), the 4-for-1 split of
August 2020, and an item 2.02 8-K that is not a results release (the letter to investors of January 2, 2019)."""

import json
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.config import load_config
from valuation_monitor.providers.sec import headline
from valuation_monitor.us_release import parse_us_release

ROOT = Path(__file__).resolve().parent.parent
AAPL = ROOT / "data" / "filings" / "AAPL"


def table(*rows):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


INCOME = table(
    ["Three Months Ended", "Twelve Months Ended"],
    ["September 26, 2026", "September 27, 2025", "September 26, 2026", "September 27, 2025"],
    ["Net sales:"],
    ["Products", "$", "84,321", "$", "73,716", "$", "320,000", "$", "307,003"],
    ["Services", "30,156", "28,750", "115,000", "109,158"],
    ["Total net sales", "114,477", "102,466", "435,000", "416,161"],
    ["Net income", "$", "30,711", "$", "27,466", "$", "120,000", "$", "112,010"],
    ["Earnings per share:"],
    ["Basic", "$", "2.11", "$", "1.85", "$", "8.20", "$", "7.49"],
    ["Diluted", "$", "2.10", "$", "1.85", "$", "8.17", "$", "7.46"],
    ["Shares used in computing earnings per share:"],
    ["Basic", "14,570,000", "14,815,000", "14,650,000", "14,948,500"],
    ["Diluted", "14,630,000", "14,870,000", "14,700,000", "15,004,697"],
)
BALANCE = table(
    ["September 26, 2026", "September 27, 2025"],
    ["Total assets", "$", "370,000", "$", "359,241"],
    ["Total shareholders' equity", "112,093", "73,733"],
    ["Total liabilities and shareholders' equity", "$", "370,000", "$", "359,241"],
)
SHARES_NOTE = "(In millions, except number of shares, which are reflected in thousands, and per-share amounts)"
OLD_SHARES_NOTE = "(In millions, except number of shares which are reflected in thousands and per share amounts)"


def release(income_note=SHARES_NOTE, balance_note="(In millions, except number of shares, which are reflected in thousands, and par value)"):
    return (
        "<html><body><p>Exhibit 99.1</p><p>Apple reports fourth quarter results</p>"
        f"<p>CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS (Unaudited)</p><p>{income_note}</p>{INCOME}"
        f"<p>CONDENSED CONSOLIDATED BALANCE SHEETS (Unaudited)</p><p>{balance_note}</p>{BALANCE}</body></html>"
    ).encode()


@pytest.mark.parametrize("note", [SHARES_NOTE, OLD_SHARES_NOTE])
def test_shares_in_thousands_under_a_note_that_excepts_them_from_the_unit(note):
    p = parse_us_release(release(note))
    assert p.period == pd.Period("2026Q3") and p.quarterly and p.currency == "USD"  # September 26: kept as calendar 2026Q3, shown as FY2026Q4
    assert p.values["revenue"] == [114477.0, 102466.0] and p.values["profit"] == [30711.0, 27466.0]
    assert p.values["eps"] == [2.11, 1.85]  # basic, not diluted
    assert p.values["wa_shares"] == pytest.approx([14570.0, 14815.0])  # millions, not 14.57 (single shares)
    assert p.values["equity"] == pytest.approx([112093.0])  # the comparison column is the end of the fiscal year (September), not a December


def test_a_note_that_excepts_shares_without_naming_their_unit_still_means_single_shares():
    """Sea's rule is unchanged: "except for number of shares" without a unit leaves the share counts in single shares."""
    p = parse_us_release(release("(Amounts in millions of US dollars except for number of shares & per share data)"))
    assert p.values["wa_shares"] == pytest.approx([14.57, 14.815])  # 14,570,000 single shares, not 14,570 million


def test_the_letter_to_investors_is_not_a_results_release():
    letter = (
        b"<html><body><p>EX-99.1 2 a8-kexhibit991122019.htm EXHIBIT 99.1</p><p>Exhibit 99.1</p><p>Letter from Tim Cook to Apple Investors</p>"
        b"<p>Apple today issued the following public letter to investors: Today we are revising our guidance for Apple's fiscal 2019 first quarter. "
        b"While it will be a number of weeks before we complete and report our final results, we wanted to get some preliminary information to you now.</p>"
        b"</body></html>"
    )
    assert headline(letter) is None
    assert headline(b"<html><body><p>Exhibit 99.1</p><p>Apple Reports First Quarter Results</p></body></html>") == "Apple Reports First Quarter Results"


def test_config():
    stock = next(s for s in load_config(ROOT / "config.yaml").stocks if s.symbol == "AAPL")
    assert stock.name == "Apple" and stock.market == "US" and stock.cik == 320193 and stock.sec_form == "8-K"
    assert stock.filing_format == "us_press_release" and stock.metrics == ["pe", "ps", "pb"] and stock.eps_basis == {}
    assert stock.fiscal_year_end == 9


@pytest.mark.parametrize("end,label", [("2026-09-26", "FY2026Q4"), ("2025-12-27", "FY2026Q1"), ("2026-03-28", "FY2026Q2"), ("2026-06-27", "FY2026Q3"),
                                       ("2022-12-31", "FY2023Q1"), ("2023-09-30", "FY2023Q4"), ("2022-09-24", "FY2022Q4")])
def test_fiscal_labels_of_apple_quarters_by_their_end_dates(end, label):
    """A quarter that ends on a Saturday near the calendar quarter end is kept under the calendar quarter ten days before it (quarter_of),
    and shown with Apple's fiscal label; the 14-week quarter to December 31, 2022 and the year ends of September 24 and 30 included."""
    from valuation_monitor.filings import period_label
    from valuation_monitor.us_release import quarter_of

    assert period_label(quarter_of(pd.Timestamp(end)), 4, 9) == label


saved = pytest.mark.skipif(not (AAPL / "index.json").exists(), reason="no saved Apple filings (the scheduled update saves them)")


@saved
def test_saved_releases_parse_and_agree_with_the_prior_year_columns():
    """Every saved release parses; net income / basic EPS matches the basic shares within 1%; every quarter's own figures agree with the
    prior-year column a year later, once EPS and shares of the releases before the 4-for-1 split of 2020-08-31 are put on the new basis."""
    index = [e for e in json.loads((AAPL / "index.json").read_text(encoding="utf-8")) if e["results"]]
    assert len(index) >= 48 and not any(e["published"].startswith("2019-01-02") for e in index)  # the letter to investors is not taken
    own, prior = {}, {}
    for e in index:
        p = parse_us_release((AAPL / e["file"]).read_bytes())
        assert p.quarterly and p.currency == "USD" and "non_ifrs_eps" not in p.values
        factor = 4 if pd.Timestamp(e["published"]) < pd.Timestamp("2020-08-31") else 1
        for i, period in enumerate((p.period, p.period - 4)):
            v = {k: p.values[k][i] for k in ("revenue", "profit", "eps", "wa_shares")}
            assert v["profit"] / v["eps"] / v["wa_shares"] == pytest.approx(1, abs=0.01), (e["file"], period)
            (own if i == 0 else prior)[period] = {**v, "eps": v["eps"] / factor, "wa_shares": v["wa_shares"] * factor}
    compared = 0
    for period, v in prior.items():
        if period in own:
            compared += 1
            assert v["revenue"] == own[period]["revenue"] and v["profit"] == own[period]["profit"], period
            assert v["eps"] == pytest.approx(own[period]["eps"], abs=0.0051), period
    assert compared >= 44
