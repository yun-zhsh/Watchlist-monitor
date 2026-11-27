"""Parse results of the various filing layouts."""

from __future__ import annotations

from typing import NamedTuple

import pandas as pd


class NoStatements(ValueError):
    """A results filing without its financial statements, so nothing can be read from it: the earnings store leaves it out (Baidu's
    fourth-quarter 2020 release on EDGAR has the summary tables only; its quarter comes from the next release's previous-quarter column)."""


class Parsed(NamedTuple):
    currency: str
    values: dict[str, list[float]]  # series -> this period, same period last year, (previous period); equity: end of this period, end of last year
    period: pd.Period  # reporting quarter
    quarterly: bool = True  # False means year-to-date cumulative figures (e.g. the full year)
    notes: dict[str, str] | None = None  # series -> what is special about its figures in this filing, shown with every TTM that uses them (SAP: non-IFRS EPS from continuing operations)
