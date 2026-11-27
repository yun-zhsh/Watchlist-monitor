"""Extract earnings data from the financial statements Brazilian listed companies deliver to CVM (cvm_statements; verified on all TOTVS
ITR and DFP documents from 2013 on).

The statements are read by CVM's standard account codes, never by their (Portuguese) labels. From the consolidated statements:
  3.01        revenue (net revenue from sales and services)
  3.11.01     net income attributable to the owners of the parent
  3.99.01.01  basic EPS of the ordinary shares (BRL per share); a company with preferred shares has more lines under 3.99.01 and is
              refused, its EPS per class being something this program does not model
  2.03        total equity (consolidated, including non-controlling interests), less
  2.03.09     non-controlling interests: the equity attributable to the owners of the parent
  shares      the share capital at the end of the period: ordinary shares less those held in treasury
An ITR (the first three quarters) has the quarter itself (three months; the first quarter's three months are its year to date) and the
same quarter of the year before; a DFP (the year) has the year and the year before (quarterly=False: the fourth quarter is the year less
the first three, see filings.build_history). Equity: the end of this period and the end of the previous year. The shares are the
period-end shares outstanding (filings.PERIOD_END_SHARES).

The extract saved for each document (providers/cvm.py) is JSON with English keys:
  {"source": ..., "kind": "ITR", "company_code": "019992", "reference_date": "2025-09-30", "version": 1, "document_id": 152679,
   "received_date": "2025-11-05", "delivered": "2025-11-05T18:21:37", "values_from": "CVM open data", "link": ...,
   "currency": "BRL", "scale": 1000,
   "income_statement": [{"code": "3.01", "start": "2025-07-01", "end": "2025-09-30", "value": 1562427.0}, ...],
   "balance_sheet": [{"code": "2.03", "end": "2025-09-30", "value": 5407082.0}, ...],
   "shares": {"ordinary": 599401581, "preferred": 0, "ordinary_treasury": 13492877, "preferred_treasury": 0}}
Amounts are in the document's unit (`scale`: 1000 for thousands of reais) and converted to millions when read; EPS is per share.

A version that was replaced later is no longer in the open data; its figures are read from the XML of the delivered document
(read_document), which comes in two layouts: the older one (an .itr / .dfp file, itself a zip, with InfoFinaDFin.xml) and the newer one
(a single XML file with one element per account).
"""

from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd

from valuation_monitor.parsed import Parsed

STATEMENTS = {"income_statement": "DRE", "balance_sheet": "BPP"}  # extract key -> CVM's name of the statement (income statement; liabilities and equity)
REVENUE, PROFIT, EPS_PARENT, EPS = "3.01", "3.11.01", "3.99.01", "3.99.01.01"
EQUITY, MINORITY = "2.03", "2.03.09"
CURRENCIES = {"REAL": "BRL"}  # CVM's currency name -> ISO code
SCALES = {"MIL": 1000, "UNIDADE": 1}  # CVM's unit of the amounts -> multiplier (thousands; units)
MILLION = 1e6
KIND_NAMES = {"ITR": "quarterly financial statements (ITR)", "DFP": "annual financial statements (DFP)"}
TITLE_RE = re.compile(r"^(?P<kind>ITR|DFP) (?:(?P<q>[1-3])Q(?P<qy>\d{2})|(?P<y>\d{4}))\b")


def wanted_code(part: str, code: str) -> bool:
    """Whether an account is kept in the extract: revenue, net income and EPS lines; total equity and non-controlling interests."""
    if part == "income_statement":
        return code == REVENUE or code == "3.11" or code.startswith("3.11.") or code == "3.99" or code.startswith("3.99.")
    return code in (EQUITY, MINORITY)


def currency_of(names: set[str]) -> str:
    if len(names) != 1 or next(iter(names)) not in CURRENCIES:
        raise ValueError(f"unexpected currency in the CVM data: {sorted(names)}")
    return CURRENCIES[next(iter(names))]


def scale_of(names: set[str]) -> int:
    if len(names) != 1 or next(iter(names)) not in SCALES:
        raise ValueError(f"unexpected unit of the amounts in the CVM data: {sorted(names)}")
    return SCALES[next(iter(names))]


def english_title(kind: str, reference: pd.Timestamp, version: int = 1) -> str:
    """"ITR 3Q25", "DFP 2025"; a later version: "ITR 1Q20 (version 2)"."""
    period = f"{reference.quarter}Q{reference:%y}" if kind == "ITR" else f"{reference.year}"
    return f"{kind} {period}" + (f" (version {version})" if version > 1 else "")


def make_extract(doc: dict, delivered: pd.Timestamp, values_from: str, extract: dict) -> dict:
    return {
        "source": f"CVM, {KIND_NAMES[doc['kind']]}: account codes and values of the consolidated statements (an extract, not the original document)",
        "kind": doc["kind"],
        "company_code": doc["company_code"],
        "reference_date": doc["reference_date"],
        "version": doc["version"],
        "document_id": doc["document_id"],
        "received_date": doc["received_date"],
        "delivered": f"{delivered:%Y-%m-%dT%H:%M:%S}",
        "values_from": values_from,
        "link": doc["link"],
        **extract,
    }


# The older layout (InfoFinaDFin.xml): one element per account and statement, the statement and the scope as codes, the figures in
# numbered columns whose meaning depends on the document.
OLD_STATEMENT_CODES = {"income_statement": "4", "balance_sheet": "3"}
OLD_CONSOLIDATED = "2"
OLD_SHARES = {  # share capital (ComposicaoCapitalSocialDemonstracaoFinanceiraNegocios.xml): element -> key in the extract
    "QuantidadeAcaoOrdinariaCapitalIntegralizado": "ordinary", "QuantidadeAcaoPreferencialCapitalIntegralizado": "preferred",
    "QuantidadeAcaoOrdinariaTesouraria": "ordinary_treasury", "QuantidadeAcaoPreferencialTesouraria": "preferred_treasury",
}
# The newer layout: the consolidated statements under one element, each account with its figures in named elements.
NEW_CONSOLIDATED = "DfConsolidadas"
NEW_STATEMENTS = {"income_statement": "DemonstracaoResultado", "balance_sheet": "BalancoPatrimonialPassivo"}
NEW_SHARES = {("CaptalIntegralizado", "Ordinarias"): "ordinary", ("CaptalIntegralizado", "Preferenciais"): "preferred",
              ("Tesouraria", "Ordinarias"): "ordinary_treasury", ("Tesouraria", "Preferenciais"): "preferred_treasury"}
NEW_CURRENCY, NEW_SCALE = {"1": "BRL"}, {"1": 1, "2": 1000}  # the codes of the currency (reais) and of the unit (units; thousands)


def _spans(kind: str, reference: pd.Timestamp) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    """The periods of the columns of an income statement: this quarter, this year to date, and the same for the year before (a DFP: the year)."""
    prior = reference - pd.DateOffset(years=1)
    year_start = pd.Timestamp(year=reference.year, month=1, day=1)
    out = {"year": (year_start, reference), "prior_year": (year_start - pd.DateOffset(years=1), prior)}
    if kind == "ITR":
        quarter = pd.Period(reference, freq="Q")
        out |= {"quarter": (quarter.start_time, reference), "prior_quarter": ((quarter - 4).start_time, prior)}
    return out


# Older layout, column number -> what it holds
OLD_COLUMNS = {
    ("ITR", "income_statement"): {2: "quarter", 4: "year", 5: "prior_quarter", 6: "prior_year"},  # the first quarter fills only the year-to-date columns
    ("DFP", "income_statement"): {1: "year", 2: "prior_year"},  # column 3: the year before that
    ("ITR", "balance_sheet"): {2: "end", 3: "prior_year_end"},
    ("DFP", "balance_sheet"): {1: "end", 2: "prior_year_end"},
}
# Newer layout, element -> what it holds
NEW_COLUMNS = {
    ("ITR", "income_statement"): {"TrimestreAtual": "quarter", "AcumuladoExercicioAtual": "year", "TrimestreAnterior": "prior_quarter",
                                  "AcumuladoExercicioAnterior": "prior_year"},
    ("DFP", "income_statement"): {"UltimoExercicio": "year", "PenultimoExercicio": "prior_year"},
    ("ITR", "balance_sheet"): {"TrimestreAtual": "end", "ExercicioAnterior": "prior_year_end"},
    ("DFP", "balance_sheet"): {"UltimoExercicio": "end", "PenultimoExercicio": "prior_year_end"},
}


def _row(part: str, code: str, column: str, kind: str, reference: pd.Timestamp, value: float) -> dict | None:
    if part == "balance_sheet":
        end = reference if column == "end" else pd.Timestamp(year=reference.year - 1, month=12, day=31)
        return {"code": code, "end": f"{end:%Y-%m-%d}", "value": value}
    if kind == "ITR" and reference.quarter == 1 and column in ("quarter", "prior_quarter"):
        return None  # the first quarter: the three months are the year to date
    start, end = _spans(kind, reference)[column]
    return {"code": code, "start": f"{start:%Y-%m-%d}", "end": f"{end:%Y-%m-%d}", "value": value}


def _sorted(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["code"], r.get("start", ""), r["end"]))


def _brazilian_number(text: str | None) -> float:
    """A figure of the newer layout: "7.665.400" or "7665400" (thousands), "0,33222" (decimal comma); empty is no figure (0)."""
    text = (text or "").strip()
    return float(text.replace(".", "").replace(",", ".")) if text else 0.0


def _read_old(inner: bytes, kind: str, reference: pd.Timestamp, currency: str, scale: int) -> dict:
    with zipfile.ZipFile(io.BytesIO(inner)) as archive:
        accounts = ET.fromstring(archive.read("InfoFinaDFin.xml"))
        capital = ET.fromstring(archive.read("ComposicaoCapitalSocialDemonstracaoFinanceiraNegocios.xml"))
    out = {"currency": currency, "scale": scale, **{part: [] for part in STATEMENTS}}
    for item in accounts:
        plan = item.find("PlanoConta")
        version = plan.find("VersaoPlanoConta")
        if version.findtext("CodigoTipoInformacaoFinanceira") != OLD_CONSOLIDATED:
            continue
        code = plan.findtext("NumeroConta")
        for part, statement in OLD_STATEMENT_CODES.items():
            if version.findtext("CodigoTipoDemonstracaoFinanceira") != statement or not wanted_code(part, code):
                continue
            for number, column in OLD_COLUMNS[(kind, part)].items():
                row = _row(part, code, column, kind, reference, float(item.findtext(f"ValorConta{number}")))
                if row is not None:
                    out[part].append(row)
    found = {tag: capital.find(f".//{tag}") for tag in OLD_SHARES}
    out["shares"] = {key: int(found[tag].text) for tag, key in OLD_SHARES.items()} if all(e is not None for e in found.values()) else None
    return {k: _sorted(v) if k in STATEMENTS else v for k, v in out.items()}


def _read_new(xml: bytes, kind: str, reference: pd.Timestamp) -> dict:
    root = ET.fromstring(xml)
    header = root.find(f"Dados{kind}")
    if header is None:
        raise ValueError(f"the document has no Dados{kind} element")
    currency, scale = NEW_CURRENCY.get(header.findtext("Moeda")), NEW_SCALE.get(header.findtext("EscalaMoeda"))
    if currency is None or scale is None:
        raise ValueError(f"unexpected currency or unit codes in the document: {header.findtext('Moeda')}, {header.findtext('EscalaMoeda')}")
    consolidated = header.find(f".//{NEW_CONSOLIDATED}")
    if consolidated is None:
        raise ValueError("the document has no consolidated statements")
    out = {"currency": currency, "scale": scale}
    for part, tag in NEW_STATEMENTS.items():
        statement = consolidated.find(tag)
        if statement is None:
            raise ValueError(f"the consolidated statements have no {tag}")
        rows = []
        for account in statement.findall("Conta"):
            code = account.findtext("CodigoConta")
            if not wanted_code(part, code):
                continue
            columns = NEW_COLUMNS[(kind, part)]
            if not any(account.find(element) is not None for element in columns):
                raise ValueError(f"account {code} has none of the columns {sorted(columns)}")
            for element, column in columns.items():
                if account.find(element) is not None:
                    row = _row(part, code, column, kind, reference, _brazilian_number(account.findtext(element)))
                    if row is not None:
                        rows.append(row)
        out[part] = _sorted(rows)
    capital = header.find(".//ComposicaoCapital")
    shares = {key: capital.findtext(f"{group}/{element}") if capital is not None else None for (group, element), key in NEW_SHARES.items()}
    out["shares"] = {key: int(_brazilian_number(v)) for key, v in shares.items()} if all(v is not None for v in shares.values()) else None
    return out


def read_document(document: bytes, kind: str, reference: pd.Timestamp, currency: str = "BRL", scale: int = 1000) -> dict:
    """The extract rows of a delivered document (zip), from its XML: the older layout (an inner .itr / .dfp zip) or the newer one (one XML
    file). currency and scale are those of the open data for the older layout, whose own codes for them are not read."""
    with zipfile.ZipFile(io.BytesIO(document)) as archive:
        names = archive.namelist()
        inner = [n for n in names if n.lower().endswith("." + kind.lower())]
        if inner:
            return _read_old(archive.read(inner[0]), kind, reference, currency, scale)
        single = [n for n in names if n.lower().endswith(".xml") and kind in n and not n.startswith("Formulario")]
        if single:
            return _read_new(archive.read(single[0]), kind, reference)
    raise ValueError(f"the document has neither an .{kind.lower()} file nor a {kind} XML file: {names}")


def _value(rows: list[dict], code: str, end: pd.Timestamp, start: pd.Timestamp | None = None) -> float | None:
    found = [
        r["value"] for r in rows
        if r["code"] == code and pd.Timestamp(r["end"]) == end and (start is None or pd.Timestamp(r["start"]) == start)
    ]
    if len(found) > 1:
        raise ValueError(f"account {code} appears more than once for {end:%Y-%m-%d}")
    return found[0] if found else None


def parse_cvm_statements(content: bytes) -> Parsed:
    data = json.loads(content)
    kind, reference = data["kind"], pd.Timestamp(data["reference_date"])
    period = pd.Period(reference, freq="Q")
    if kind == "DFP" and period.quarter != 4:
        raise ValueError(f"a DFP for {reference:%Y-%m-%d}, not a year end")
    if kind == "ITR" and period.quarter == 4:
        raise ValueError(f"an ITR for {reference:%Y-%m-%d}, a year end")
    unit = data["scale"] / MILLION
    income, balance = data["income_statement"], data["balance_sheet"]
    prior_end = reference - pd.DateOffset(years=1)
    spans = [
        (period.start_time if kind == "ITR" else pd.Timestamp(year=reference.year, month=1, day=1), reference),
        ((period - 4).start_time if kind == "ITR" else pd.Timestamp(year=reference.year - 1, month=1, day=1), prior_end),
    ]

    eps_lines = sorted({r["code"] for r in income if r["code"].startswith(EPS_PARENT + ".") and r["value"]})
    if eps_lines != [EPS]:
        raise ValueError(f"expected a basic EPS of the ordinary shares only, got the lines {eps_lines}")

    values: dict[str, list[float]] = {}
    for series, code, scale in (("revenue", REVENUE, unit), ("profit", PROFIT, unit), ("eps", EPS, 1.0)):
        found = [_value(income, code, end, start) for start, end in spans]
        if found[0] is None:
            raise ValueError(f"account {code} ({series}) is missing for {reference:%Y-%m-%d}")
        values[series] = [v * scale for v in found if v is not None][: 2 if found[1] is not None else 1]

    year_end = pd.Timestamp(year=reference.year - 1, month=12, day=31)
    equity = []
    for end in (reference, year_end):
        total, minority = _value(balance, EQUITY, end), _value(balance, MINORITY, end)
        if total is None:
            if end == reference:
                raise ValueError(f"the total equity (account {EQUITY}) is missing for {end:%Y-%m-%d}")
            break
        equity.append((total - (minority or 0.0)) * unit)
    values["equity"] = equity

    shares = data.get("shares")
    if shares:
        if shares.get("preferred"):
            raise ValueError("the company has preferred shares, which this program does not model")
        values["wa_shares"] = [(shares["ordinary"] - shares["ordinary_treasury"]) / MILLION]
    return Parsed(data["currency"], values, period, quarterly=kind == "ITR")
