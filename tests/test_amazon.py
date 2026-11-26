"""Amazon.com (AMZN): 8-K press releases in the us_press_release layout. Headlines before 2020 announce the quarter's sales
("AMAZON.COM ANNOUNCES THIRD QUARTER SALES UP 24% TO $ 70.0 BILLION"), the newer statements list last year's column first,
and the 20-for-1 split of June 2022 is restated in the prior-year columns of the releases published after it."""

import json
from pathlib import Path

import pandas as pd
import pytest

from valuation_monitor.config import load_config
from valuation_monitor.providers.sec import headline
from valuation_monitor.us_release import parse_us_release

ROOT = Path(__file__).resolve().parent.parent
AMZN = ROOT / "data" / "filings" / "AMZN"
SPLIT_DAY = "2022-06-06"  # Yahoo's record of the 20-for-1 split


def page(text: str) -> bytes:
    return f"<html><body><p>{text}</p>".encode() + b"<p>x</p>" * 5000 + b"</body></html>"


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "EX-99.1 2 amzn-2019930xex991.htm EXHIBIT 99.1 Exhibit Exhibit 99.1 AMAZON.COM ANNOUNCES THIRD QUARTER SALES UP 24% TO $ 70.0 BILLION SEATTLE—(BUSINESS WIRE) October 24, 2019",
            "AMAZON.COM ANNOUNCES THIRD QUARTER SALES UP 24% TO $ 70.0 BILLION",
        ),
        (
            "EX-99.1 2 amzn-20141231xex991.htm EXHIBIT 99.1 AMZN-2014.12.31-EX99.1 Exhibit 99.1 AMAZON.COM ANNOUNCES FOURTH QUARTER SALES UP 15% TO $ 29.33 BILLION SEATTLE",
            "AMAZON.COM ANNOUNCES FOURTH QUARTER SALES UP 15% TO $ 29.33 BILLION",
        ),
        (
            "EX-99.1 2 amzn-20200331xex991.htm EXHIBIT 99.1 Exhibit Exhibit 99.1 AMAZON.COM ANNOUNCES FIRST QUARTER RESULTS SEATTLE",
            "AMAZON.COM ANNOUNCES FIRST QUARTER RESULTS",
        ),
        (
            "EX-99.1 2 amzn-20200630xex991.htm EXHIBIT 99.1 Document Exhibit 99.1 AMAZON.COM ANNOUNCES SECOND QUARTER RESULTS SEATTLE",
            "AMAZON.COM ANNOUNCES SECOND QUARTER RESULTS",
        ),
        ("AMAZON.COM ANNOUNCES THIRD QUARTER RESULTS SEATTLE", "AMAZON.COM ANNOUNCES THIRD QUARTER RESULTS"),
        ("AMAZON.COM ANNOUNCES FINANCIAL RESULTS SEATTLE", "AMAZON.COM ANNOUNCES FINANCIAL RESULTS"),
        ("AMAZON.COM ANNOUNCES DATE OF ANNUAL MEETING", None),
        # the label is removed only before Amazon's headline: other companies' titles keep what they had
        ("EX-99.1 2 a.htm EXHIBIT 99.1 Exhibit 99.1 Sea Limited Reports First Quarter 2019 Results", "Exhibit 99.1 Sea Limited Reports First Quarter 2019 Results"),
    ],
)
def test_headline(text, expected):
    assert headline(page(text)) == expected


def table(*rows):
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table>{body}</table>"


UNIT = "<p>(in millions, except per share data)</p>"
CASH_FLOWS = table(  # the first statement of the release: net income, but no revenue and EPS
    ["Three Months Ended September 30,", "Nine Months Ended September 30,", "Twelve Months Ended September 30,"],
    ["2025", "2026", "2025", "2026", "2025", "2026"],
    ["Net income", "21,187", "20,685", "56,478", "113,587", "70,557", "133,779"],
)
INCOME = table(  # last year first, as in the releases since 2016
    ["Three Months Ended September 30,", "Nine Months Ended September 30,"],
    ["2025", "2026", "2025", "2026"],
    ["Net product sales", "74,058", "75,296", "206,274", "224,202"],
    ["Net service sales", "106,111", "126,648", "297,264", "359,867"],
    ["Total net sales", "180,169", "201,944", "503,538", "584,069"],
    ["Net income", "21,187", "20,685", "56,478", "113,587"],
    ["Basic earnings per share", "1.98", "1.92", "5.31", "10.55"],
    ["Diluted earnings per share", "1.95", "1.89", "5.23", "10.40"],
    ["Weighted-average shares used in computation of earnings per share:"],
    ["Basic", "10,674", "10,797", "10,638", "10,770"],
    ["Diluted", "10,872", "10,954", "10,807", "10,923"],
)
BALANCE = table(
    ["December 31, 2025", "September 30, 2026"],
    ["(unaudited)"],
    ["Total assets", "818,042", "1,100,000"],
    ["Total stockholders' equity", "411,065", "613,601"],
)


def test_amazon_statements():
    p = parse_us_release(f"<html><body><p>Exhibit 99.1</p>{UNIT}{CASH_FLOWS}{UNIT}{INCOME}{UNIT}{BALANCE}</body></html>".encode())
    assert p.period == pd.Period("2026Q3") and p.quarterly and p.currency == "USD"
    assert p.values["revenue"] == [201944.0, 180169.0] and p.values["profit"] == [20685.0, 21187.0]
    assert p.values["eps"] == [1.92, 1.98] and p.values["wa_shares"] == [10797.0, 10674.0]  # basic, not diluted
    assert p.values["equity"] == [613601.0, 411065.0]
    assert "non_ifrs_eps" not in p.values


def test_config():
    stocks = load_config(ROOT / "config.yaml").stocks
    stock = next(s for s in stocks if s.symbol == "AMZN")
    assert stock.name == "Amazon.com" and stock.market == "US" and stock.cik == 1018724 and stock.sec_form == "8-K"
    assert stock.filing_format == "us_press_release" and stock.metrics == ["pe", "ps", "pb"] and stock.eps_basis == {}


saved = pytest.mark.skipif(not (AMZN / "index.json").exists(), reason="no saved Amazon filings (the scheduled update saves them)")


def saved_releases():
    index = [e for e in json.loads((AMZN / "index.json").read_text(encoding="utf-8")) if e["results"]]
    return [(e, parse_us_release((AMZN / e["file"]).read_bytes())) for e in index]


@saved
def test_every_saved_amazon_release_parses():
    releases = saved_releases()
    periods = [p.period for _, p in releases]
    assert periods[0] == pd.Period("2014Q4") and periods[-1] >= pd.Period("2026Q3")
    assert periods == list(pd.period_range(periods[0], periods[-1], freq="Q"))  # no quarter missing: the sales headlines until 2019 are found
    for e, p in releases:
        assert e["title"].startswith("AMAZON.COM ANNOUNCES "), e["title"]
        assert p.currency == "USD" and p.quarterly and "non_ifrs_eps" not in p.values, p.period
        for col in (0, 1):
            profit, eps, shares = p.values["profit"][col], p.values["eps"][col], p.values["wa_shares"][col]
            # basic EPS = net income / basic shares, within the rounding of EPS to the cent and of the amounts to millions
            # (2014Q3: -437 / 463 = -0.944 against -0.95)
            assert abs(profit / shares - eps) <= 0.005 + 0.01 * abs(eps), (p.period, col)


@saved
def test_prior_year_columns_agree_on_the_split_adjusted_basis():
    """Each release restates the prior-year column for the 20-for-1 split of June 2022 if it was published after it; on the current basis
    every quarter's revenue, net income and EPS agree between its own release and the release a year later."""
    own, prior = {}, {}
    for e, p in saved_releases():
        factor = 20.0 if e["published"] < SPLIT_DAY else 1.0
        own[p.period] = [p.values["revenue"][0], p.values["profit"][0], p.values["eps"][0] / factor]
        prior[p.period - 4] = [p.values["revenue"][1], p.values["profit"][1], p.values["eps"][1] / factor]
    common = sorted(set(own) & set(prior))
    assert len(common) >= 44
    for period in common:
        assert own[period][:2] == pytest.approx(prior[period][:2], abs=0.5), period
        assert abs(own[period][2] - prior[period][2]) <= 0.006, period


@saved
def test_amazon_known_values():
    by_period = {p.period: p for _, p in saved_releases()}
    assert by_period[pd.Period("2026Q3")].values["eps"] == [1.92, 1.98]
    assert by_period[pd.Period("2022Q1")].values["eps"] == [-7.56, 16.09]  # before the split
    assert by_period[pd.Period("2022Q2")].values["eps"] == [-0.2, 0.77]  # after it: 2021Q2 15.40 / 20
    assert by_period[pd.Period("2014Q4")].values["equity"] == [10741.0, 9746.0]
