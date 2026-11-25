"""The Non-GAAP (adjusted) EPS of an earnings press release that is read alongside a company's financial statements (XP Inc.).

XP's earnings come from its IFRS statements (ifrs.py), which have only the reported figures. Its press release, furnished on Form 6-K
(until 2021 as another exhibit of the 6-K with the statements, from 2022 on in a 6-K of its own, minutes or hours apart), states the adjusted
EPS in its text since the fourth quarter of 2024:

  "Adjusted Net Income and Adjusted EPS In 4Q24, Adjusted Net Income reached a record R$1.2 billion , showing a 2% QoQ and a 16% rise YoY.
   Adjusted Basic EPS was R$2.25, reflecting a 2% QoQ growth and an 18% YoY increase. Adjusted diluted EPS was R$2.23 for the quarter, ..."

The basic figure of the quarter named in the "In 4Q24," sentence is taken (the full-year figures that follow in fourth-quarter releases are not).
A release that states no Adjusted Basic EPS gives no Non-GAAP EPS for its quarter: XP before the fourth quarter of 2024 (only an adjusted net income,
without a per-share figure, until 2023; no adjustments in 2024), and quarters without adjustments (2Q25, 3Q25: "Basic EPS was R$2.50"). The reported
EPS is never taken in its place and nothing is worked out from the adjusted net income: such a quarter stays empty.

The reporting quarter of a release without an adjusted EPS comes from its title ("XP Inc. Reports Third Quarter 2026 Results", "... 2Q20 ...") or,
when the title has no year ("XP Inc. Reports Second Quarter Financial Results", 2022), from the first quarter label ("2Q22") of the text.
"""

from __future__ import annotations

import html as htmllib
import re

import pandas as pd

from valuation_monitor.parsed import Parsed

ADJUSTED_EPS_RE = re.compile(
    r"\bIn ([1-4])Q(\d\d), Adjusted Net Income\b.{0,300}?\bAdjusted Basic EPS was R\$ ?(\d+(?:\.\d+)?)\b(?!\.?\d)", re.S
)
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
TITLE_RE = re.compile(r"\bReports (?:(?P<ordinal>First|Second|Third|Fourth) Quarter(?: and Full[- ]Year)?(?: (?P<year>20\d\d))?|(?P<q>[1-4])Q(?P<yy>\d\d))\b", re.I)
QUARTER_LABEL_RE = re.compile(r"\b([1-4])Q(\d\d)\b")
CURRENCY = "BRL"


def is_results_release_title(title: str) -> bool:
    """Whether a press release title announces the results of a quarter ("XP Inc. Reports Fourth Quarter 2024 Results", "... Reports 2Q20 ...")."""
    return TITLE_RE.search(title) is not None


def text_of(content: bytes) -> str:
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", content.decode("utf-8", "replace")))
    return " ".join(text.replace("\xa0", " ").split())


def release_period(text: str) -> pd.Period:
    """The reporting quarter a release is for, from its title or else the first quarter label of its text."""
    title = TITLE_RE.search(text[:2000])
    if title is not None and title.group("q"):
        return pd.Period(year=2000 + int(title.group("yy")), quarter=int(title.group("q")), freq="Q")
    if title is not None and title.group("year"):
        return pd.Period(year=int(title.group("year")), quarter=ORDINALS[title.group("ordinal").lower()], freq="Q")
    label = QUARTER_LABEL_RE.search(text[:20000])
    if title is not None and label is not None and int(label.group(1)) == ORDINALS[title.group("ordinal").lower()]:
        return pd.Period(year=2000 + int(label.group(2)), quarter=int(label.group(1)), freq="Q")
    raise ValueError("Cannot tell the reporting quarter of the press release from its title")


def parse_adjusted_eps_release(content: bytes) -> Parsed:
    """The Adjusted Basic EPS (R$ per share) of the quarter of the release, as `non_ifrs_eps`; no values when the release states none."""
    text = text_of(content)
    period = release_period(text)
    m = ADJUSTED_EPS_RE.search(text)
    if m is None:
        return Parsed(CURRENCY, {}, period)
    stated = pd.Period(year=2000 + int(m.group(2)), quarter=int(m.group(1)), freq="Q")
    if stated != period:
        raise ValueError(f"The press release for {period} states the Adjusted Basic EPS of {stated}")
    return Parsed(CURRENCY, {"non_ifrs_eps": [float(m.group(3))]}, period)
