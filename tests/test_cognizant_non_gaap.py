"""Cognizant's Non-GAAP diluted EPS (us_release.py) and a change of a company's Non-GAAP definition (non_gaap_changes, build_history)."""

import json
from pathlib import Path

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from valuation_monitor.config import parse_config
from valuation_monitor.filings import Announcement, build_history
from valuation_monitor.us_release import cognizant_non_gaap_row, parse_us_release

ROOT = Path(__file__).resolve().parent.parent
FOLDER = ROOT / "data" / "filings" / "CTSH"


def table(*rows):
    return "<table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"


def reconciliation(quarter_end, years, *rows):
    return table([f"Three Months Ended {quarter_end},", "Twelve Months Ended December 31,"], years, ["GAAP income from operations", "693", "657"], *rows)


def soup(*tables):
    return BeautifulSoup("<html><body>" + "".join(tables) + "</body></html>", "lxml")


def test_the_three_months_figures_of_the_reconciliation_row_are_taken():
    old = reconciliation("March 31", ["2015", "2014"], ["GAAP diluted earnings per share", "0.62", "0.57"], ["Non-GAAP diluted earnings per share", "0.71", "0.62"])
    assert cognizant_non_gaap_row(soup(old), pd.Period("2015Q1")) == ("Non-GAAP diluted earnings per share", [0.71, 0.62])
    new = table(
        ["(dollars in millions, except per share amounts)", "Three Months Ended September 30,", "Nine Months Ended September 30,", "Guidance"],
        ["2024", "2023", "2024", "2023", "Full Year 2024 (1)"],
        ["GAAP diluted earnings per share", "1.17", "1.04", "3.41", "3.09"],
        ["Adjusted Diluted Earnings Per Share", "1.25", "1.16", "3.55", "3.37", "$4.63 - $4.67"],
    )
    assert cognizant_non_gaap_row(soup(new), pd.Period("2024Q3")) == ("Adjusted Diluted Earnings Per Share", [1.25, 1.16])


def test_the_new_measure_is_taken_when_a_release_has_both_and_other_tables_or_quarters_do_not_count():
    both = reconciliation(
        "December 31", ["2018 (a)", "2017 (a)", "2018 (a)", "2017 (a)", "Full Year 2019"],
        ["Adjusted Diluted Earnings Per Share", "0.98", "0.96", "4.02", "3.42", "at least $4.40"],
        ["Non-GAAP diluted earnings per share", "1.13", "1.03", "4.57", "3.77"],
    )
    assert cognizant_non_gaap_row(soup(both), pd.Period("2018Q4"))[1] == [0.98, 0.96]
    assert cognizant_non_gaap_row(soup(both), pd.Period("2019Q4")) is None  # other years
    highlights = table(["$ in billions, except per share data", "Q3 2024", "Q3 2023"], ["Revenue", "$5.04", "$4.90"], ["Adjusted Diluted EPS 1", "$1.25", "$1.16"])
    assert cognizant_non_gaap_row(soup(highlights), pd.Period("2024Q3")) is None
    disagree = reconciliation("March 31", ["2015", "2014"], ["Non-GAAP diluted earnings per share", "0.72", "0.62"])
    with pytest.raises(ValueError, match="disagree"):
        cognizant_non_gaap_row(soup(reconciliation("March 31", ["2015", "2014"], ["Non-GAAP diluted earnings per share", "0.71", "0.62"]), disagree), pd.Period("2015Q1"))


def ann(period, published, eps, non_gaap):
    return Announcement(pd.Period(period, freq="Q"), pd.Timestamp(published), f"https://example.com/{period}.htm", "USD", True, {"eps": [eps, eps], "non_ifrs_eps": non_gaap}, "US")


# Cognizant's figures around the change: the old measure until the third quarter of 2018, the new one from the fourth quarter of 2018 on
AROUND_THE_CHANGE = [
    ann("2017Q4", "2018-02-07 06:11", 0.5, [1.03, 0.87]),
    ann("2018Q1", "2018-05-07 06:18", 0.5, [1.06, 0.84]),
    ann("2018Q2", "2018-08-02 06:09", 0.5, [1.19, 0.93]),
    ann("2018Q3", "2018-10-30 06:07", 0.5, [1.19, 0.98]),
    ann("2018Q4", "2019-02-06 07:04", 0.5, [0.98, 0.96]),
    ann("2019Q1", "2019-05-02 16:02", 0.5, [0.91, 0.94]),
    ann("2019Q2", "2019-07-31 16:05", 0.5, [0.94, 1.05]),
    ann("2019Q3", "2019-10-30 16:05", 0.5, [1.08, 1.05]),
    ann("2019Q4", "2020-02-05 16:29", 0.5, [1.07, 0.98]),
]


def test_a_ttm_takes_four_quarters_of_one_definition_and_is_empty_until_the_new_one_has_them():
    history = build_history(AROUND_THE_CHANGE, None, ("2019-02-06",))
    ttm = history.ttm.set_index("report_date")["non_ifrs_eps_ttm"]
    assert ttm[pd.Timestamp("2018-09-30")] == pytest.approx(1.03 + 1.06 + 1.19 + 1.19)  # all old
    assert ttm[pd.Timestamp("2018-12-31"):pd.Timestamp("2019-06-30")].isna().all()  # the new one has fewer than four quarters
    assert ttm[pd.Timestamp("2019-09-30")] == pytest.approx(0.98 + 0.91 + 0.94 + 1.08)
    assert ttm[pd.Timestamp("2019-12-31")] == pytest.approx(0.91 + 0.94 + 1.08 + 1.07)
    q = history.quarters["non_ifrs_eps"]
    assert q[pd.Period("2018Q1")].value == 1.06 and len(q[pd.Period("2018Q1")].observations) == 1  # the new 0.94 is not a restatement of it
    assert q[pd.Period("2018Q4")].value == 0.98 and len(q[pd.Period("2018Q4")].observations) == 2  # confirmed by the 2019Q4 release
    notices = history.discrepancies()
    assert not [n for n in notices if "restatement" in n]
    assert any("changed the definition of its Non-GAAP EPS with the filing published 2019-02-06" in n and "until the filing published 2019-10-30 16:05" in n for n in notices)


def test_without_a_change_the_same_figures_are_restatements_and_the_ttm_mixes_them():
    history = build_history(AROUND_THE_CHANGE)
    assert history.ttm.set_index("report_date")["non_ifrs_eps_ttm"][pd.Timestamp("2018-12-31")] == pytest.approx(1.06 + 1.19 + 1.19 + 0.98)
    assert any("Non-IFRS EPS 2018Q1 differs between filings" in n for n in history.discrepancies())


def test_non_gaap_changes_are_days_and_need_a_non_gaap_pe():
    base = {"symbol": "CTSH", "cik": 1058290, "sec_form": "8-K", "filing_format": "us_press_release"}
    stock = parse_config({"stocks": [{**base, "metrics": ["pe", "pe_non_gaap"], "non_gaap_changes": [pd.Timestamp("2019-02-06").date()]}]}).stocks[0]
    assert stock.non_gaap_changes == ("2019-02-06",)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_config({"stocks": [{**base, "metrics": ["pe", "pe_non_gaap"], "non_gaap_changes": ["February 2019"]}]})
    with pytest.raises(ValueError, match="pe_non_gaap"):
        parse_config({"stocks": [{**base, "metrics": ["pe"], "non_gaap_changes": ["2019-02-06"]}]})


# ---- the saved releases (data/filings/CTSH) ----------------------------------------------------------------------------------------------

saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved Cognizant releases")


@saved
def test_every_saved_release_has_the_measure_of_its_time_and_the_config_dates_the_change():
    config = parse_config({"stocks": [s for s in __import__("yaml").safe_load((ROOT / "config.yaml").read_text())["stocks"] if s["symbol"] == "CTSH"]})
    (change,) = [pd.Timestamp(d) for d in config.stocks[0].non_gaap_changes]
    index = [e for e in json.loads((FOLDER / "index.json").read_text(encoding="utf-8")) if e["results"]]
    assert len(index) >= 49
    for e in index:
        html = (FOLDER / e["file"]).read_bytes()
        parsed = parse_us_release(html, "diluted")
        label, values = cognizant_non_gaap_row(BeautifulSoup(html, "lxml"), parsed.period)
        assert parsed.values["non_ifrs_eps"] == values
        new = pd.Timestamp(e["published"]) >= change
        assert label == ("Adjusted Diluted Earnings Per Share" if new else "Non-GAAP diluted earnings per share"), e["file"]
    assert parse_us_release((FOLDER / "2019-02-06_exhibit991123118.htm").read_bytes(), "diluted").values["non_ifrs_eps"] == [0.98, 0.96]
    assert "non_ifrs_eps" not in parse_us_release((FOLDER / "2019-02-06_exhibit991123118.htm").read_bytes()).values  # only for a stock with a Non-GAAP PE
