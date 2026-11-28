"""CVM (Brazil's Securities and Exchange Commission): the quarterly (ITR) and annual (DFP) financial statements of
companies listed on B3, as delivered to CVM.

Two public CVM services are used, both plain downloads:
  - the open data portal (dados.cvm.gov.br): one zip per year and document kind with the statements of every listed company as CSV
    files, read by CVM's standard account codes. It lists every version of a document (a restated one is delivered again under the
    same reference date with a higher version number) with its delivery day, but keeps the figures of the latest version only. The
    portal rebuilds its files about once a week, so a new quarter shows up here up to a week after it was delivered.
  - the document itself (LINK_DOC of the open data, on rad.cvm.gov.br): a zip of the delivered document, whose form records the exact
    delivery time (Brasilia time) and which holds the statements of that version as XML. Every version is downloaded once for its
    delivery time; the figures of a version that was later replaced are read from its XML (see cvm_statements.read_document).

Only an extract is saved in data/filings/<symbol>/ (JSON, see cvm_statements): the account codes and values the parser reads, with the
CVM metadata of the document. These are not the original documents (those are Portuguese, and several megabytes each); the source
links of each extract point to them. index.json records every saved document; datasets.json records the last-modified time of each
open-data file already read, so a file is downloaded again only when CVM has rebuilt it (and files of closed years, see RECHECK_YEARS,
are not checked again at all).

Partial failure: each document is registered in the index as soon as its extract is written. A document that cannot be read is an
error for that document only (reported, tried again on the next run); an open-data file whose documents were not all saved is read
again on the next run.

Publication time: the delivery time in the document's form; delivered after the B3 close (as TOTVS always has), the figures count from
the next trading day. Without it (never seen) the document is not saved.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from valuation_monitor import cvm_statements as cs
from valuation_monitor.providers.base import Filing, Release, save_index
from valuation_monitor.providers.http import HttpClient

DATASETS = {
    "DFP": "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS/",  # annual statements, one zip per year
    "ITR": "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS/",  # quarterly statements of the first three quarters
}
COMPANIES = {"TOTS3.SA": "019992"}  # Yahoo symbol -> CVM company code (CD_CVM)
FIRST_YEAR = 2010  # the first open-data year (DFP 2010; ITR from 2011)
RECHECK_YEARS = 3  # files of the current year and the two before are read again when CVM rebuilds them (a restatement: TOTVS refiled its 2020 ITRs in May 2022)
LISTING_RE = re.compile(
    r'href="(?P<name>(?:itr|dfp)_cia_aberta_(?P<year>\d{4})\.zip)">[^<]*</a>\s+(?P<modified>\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})'
)
# Columns of the open-data CSV files (CVM's own field names)
COMPANY, CNPJ, REFERENCE, VERSION, DOC_ID, RECEIVED, LINK, CATEGORY = "CD_CVM", "CNPJ_CIA", "DT_REFER", "VERSAO", "ID_DOC", "DT_RECEB", "LINK_DOC", "CATEG_DOC"
START, END, CODE, VALUE, CURRENCY, SCALE = "DT_INI_EXERC", "DT_FIM_EXERC", "CD_CONTA", "VL_CONTA", "MOEDA", "ESCALA_MOEDA"
SHARE_COLUMNS = {  # share capital: column -> key in the extract
    "QT_ACAO_ORDIN_CAP_INTEGR": "ordinary", "QT_ACAO_PREF_CAP_INTEGR": "preferred",
    "QT_ACAO_ORDIN_TESOURO": "ordinary_treasury", "QT_ACAO_PREF_TESOURO": "preferred_treasury",
}
CSV_ENCODING = "latin-1"
DELIVERY_RE = re.compile(rb"<DataEntrega>((?!0001-)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")  # the form also has empty dates (0001-01-01)
FORM_NAMES = {"ITR": "FormularioDemonstracaoFinanceiraITR.xml", "DFP": "FormularioDemonstracaoFinanceiraDFP.xml"}  # the form of the delivered document
PUBLISHED_NOTE = "delivery time of the document to CVM (Brasilia time), as recorded in the delivered document"


def parse_listing(html: str) -> dict[int, tuple[str, str]]:
    """An open-data directory page -> {year: (file name, last-modified time as listed)}."""
    return {int(m.group("year")): (m.group("name"), m.group("modified")) for m in LISTING_RE.finditer(html)}


def _csv_rows(archive: zipfile.ZipFile, name: str, keep) -> list[dict]:
    if name not in archive.namelist():
        return []
    with archive.open(name) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding=CSV_ENCODING, newline=""), delimiter=";")
        return [row for row in reader if keep(row)]


def read_dataset(data: bytes, kind: str, year: int, company: str) -> list[dict]:
    """The documents of one company in one open-data zip: one dict per version, oldest first, with its metadata and, for the version whose
    figures the file holds (the latest), its extract rows (cvm_statements.EXTRACT_FIELDS); None for an earlier version."""
    prefix = f"{kind.lower()}_cia_aberta"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        documents = _csv_rows(archive, f"{prefix}_{year}.csv", lambda r: r[COMPANY] == company and r[CATEGORY] == kind)
        if not documents:
            return []
        cnpjs = {r[CNPJ] for r in documents}
        statements = {
            part: _csv_rows(archive, f"{prefix}_{file_part}_con_{year}.csv", lambda r, part=part: r[COMPANY] == company and cs.wanted_code(part, r[CODE]))
            for part, file_part in cs.STATEMENTS.items()
        }
        shares = _csv_rows(archive, f"{prefix}_composicao_capital_{year}.csv", lambda r: r[CNPJ] in cnpjs)

    out = []
    for d in sorted(documents, key=lambda r: (r[REFERENCE], int(r[VERSION]))):
        key = (d[REFERENCE], d[VERSION])
        rows = {part: [r for r in statements[part] if (r[REFERENCE], r[VERSION]) == key] for part in cs.STATEMENTS}
        share_rows = [r for r in shares if (r[REFERENCE], r[VERSION]) == key]
        extract = None
        if any(rows.values()):
            extract = {
                "currency": cs.currency_of({r[CURRENCY] for part in rows.values() for r in part}),
                "scale": cs.scale_of({r[SCALE] for part in rows.values() for r in part}),
                **{
                    part: [
                        {"code": r[CODE], **({"start": r[START]} if START in r else {}), "end": r[END], "value": float(r[VALUE])}
                        for r in sorted(rows[part], key=lambda r: (r[CODE], r.get(START, ""), r[END]))
                    ]
                    for part in cs.STATEMENTS
                },
                "shares": {key: int(share_rows[0][column]) for column, key in SHARE_COLUMNS.items()} if share_rows else None,
            }
        out.append({
            "kind": kind,
            "company_code": company,
            "reference_date": d[REFERENCE],
            "version": int(d[VERSION]),
            "document_id": int(d[DOC_ID]),
            "received_date": d[RECEIVED],
            "link": d[LINK].replace("http://", "https://", 1),
            "extract": extract,
        })
    return out


def delivery_time(document: bytes, kind: str) -> pd.Timestamp:
    """The delivery time recorded in the form of a downloaded document (zip)."""
    with zipfile.ZipFile(io.BytesIO(document)) as archive:
        m = DELIVERY_RE.search(archive.read(FORM_NAMES[kind]))
    if m is None:
        raise ValueError("the document's form records no delivery time")
    return pd.Timestamp(m.group(1).decode())


class CvmProvider:
    def __init__(self, http: HttpClient | None, archive_dir: str | Path = "data/filings"):
        self.http = http
        self.archive_dir = Path(archive_dir)

    def release_info(self, symbol: str) -> dict[str, Release]:
        """Every figure comes from the document whose delivery time is its publication time: always empty."""
        return {}

    def _load(self, folder: Path) -> tuple[list[dict], dict[str, str]]:
        index_path, state_path = folder / "index.json", folder / "datasets.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        return index, state

    def results_announcements(self, symbol: str, since: datetime, now: datetime | None = None) -> list[Filing]:
        """The ITR and DFP documents delivered since `since`, in order of delivery (new ones are fetched first)."""
        folder = self.archive_dir / symbol.upper()
        index, state = self._load(folder)
        try:
            self._fetch_new(symbol, since, folder, index, state, now or datetime.now())
        except Exception as e:
            if not index:
                raise
            print(f"{symbol}: cannot reach the CVM open data ({e}); using the {len(index)} saved documents", file=sys.stderr)

        since_ts = pd.Timestamp(since)
        filings = []
        for e in sorted(index, key=lambda e: (e["published"], e["document_id"])):
            if pd.Timestamp(e["published"]) < since_ts:
                continue
            note = PUBLISHED_NOTE
            if e["version"] > 1:
                first = min((x for x in index if (x["kind"], x["reference_date"]) == (e["kind"], e["reference_date"])), key=lambda x: x["version"])
                note += f"; version {e['version']} of the {e['kind']}, restating the one first delivered {pd.Timestamp(first['published']):%Y-%m-%d %H:%M}"
            filings.append(
                Filing(
                    title=e["title"],
                    published=pd.Timestamp(e["published"]),
                    url=e["url"],
                    content=(folder / e["file"]).read_bytes(),
                    note=note,
                    correction=e["version"] > 1,
                )
            )
        return filings

    def _fetch_new(self, symbol: str, since: datetime, folder: Path, index: list[dict], state: dict[str, str], now: datetime) -> None:
        company = COMPANIES.get(symbol.upper())
        if company is None:
            raise ValueError(f"{symbol}: no CVM company code is known (add it to COMPANIES in providers/cvm.py)")
        known = {e["document_id"] for e in index}
        first_year = max(FIRST_YEAR, pd.Timestamp(since).year - 1)  # one more year: the comparatives and the year-to-date figures need it
        for kind, base in DATASETS.items():
            listing = parse_listing(self.http.get_bytes(base).decode("utf-8", "replace"))
            for year, (name, modified) in sorted(listing.items()):
                closed = year <= now.year - RECHECK_YEARS
                if year < first_year or state.get(name) == modified or (closed and name in state):
                    continue
                try:
                    documents = read_dataset(self.http.get_bytes(base + name, immutable=closed), kind, year, company)
                except Exception as e:
                    print(f"ERROR {symbol}: CVM open data {name} cannot be read ({type(e).__name__}: {e}); tried again on the next update", file=sys.stderr)
                    continue
                complete = True
                for doc in documents:
                    if doc["document_id"] in known:
                        continue
                    try:
                        entry = self._save_document(symbol, folder, doc)
                    except Exception as e:
                        print(
                            f"ERROR {symbol}: {doc['kind']} {doc['reference_date']} version {doc['version']} ({doc['link']}) cannot be read "
                            f"({type(e).__name__}: {e}); not saved, tried again on the next update",
                            file=sys.stderr,
                        )
                        complete = False
                        continue
                    index.append(entry)
                    known.add(doc["document_id"])
                    index.sort(key=lambda e: (e["published"], e["document_id"]))
                    save_index(folder / "index.json", index)  # saved as it grows: a later failure does not lose the documents already fetched
                if complete:
                    state[name] = modified
                    save_index(folder / "datasets.json", dict(sorted(state.items())))

    def _save_document(self, symbol: str, folder: Path, doc: dict) -> dict:
        """Download the delivered document for its delivery time (and the figures of a replaced version), write the extract; return its index entry."""
        document = self.http.get_bytes(doc["link"], cache=False)
        published = delivery_time(document, doc["kind"])
        if f"{published:%Y-%m-%d}" != doc["received_date"]:
            print(f"{symbol}: {doc['kind']} {doc['reference_date']} version {doc['version']}: delivered {published}, the open data says {doc['received_date']}", file=sys.stderr)
        extract = doc["extract"]
        source = "CVM open data"
        if extract is None:  # a version replaced later: the open data keeps only the latest figures
            extract = cs.read_document(document, doc["kind"], pd.Timestamp(doc["reference_date"]))
            source = "the delivered document"
        elif extract["shares"] is None:  # the open data has no share capital for TOTVS before 2020: the delivered document has it
            extract = {**extract, "shares": cs.read_document(document, doc["kind"], pd.Timestamp(doc["reference_date"]))["shares"]}
            source = "CVM open data; the share capital from the delivered document"
        content = cs.make_extract(doc, published, source, extract)
        title = cs.english_title(doc["kind"], pd.Timestamp(doc["reference_date"]), doc["version"])
        name = f"{doc['reference_date']}_{doc['kind']}_v{doc['version']}_{doc['document_id']}.json"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps(content, indent=1) + "\n", encoding="utf-8")
        print(f"{symbol}: new document {title}, delivered {published} ({source})", file=sys.stderr)
        return {
            "kind": doc["kind"],
            "reference_date": doc["reference_date"],
            "version": doc["version"],
            "document_id": doc["document_id"],
            "title": title,
            "published": f"{published:%Y-%m-%dT%H:%M:%S}",
            "url": doc["link"],
            "file": name,
        }
