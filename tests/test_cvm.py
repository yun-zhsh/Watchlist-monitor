"""TOTVS (TOTS3.SA, Brazil): the CVM provider (open data and delivered documents) and the parser of its extracts (cvm_statements), on
synthetic files and on the extracts saved in the repo (data/filings/TOTS3.SA)."""

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import requests

from valuation_monitor import cvm_statements as cs
from valuation_monitor.config import DEFAULT_MAX_PRICE_AGE_DAYS, load_config, parse_config
from valuation_monitor.filings import PERIOD_END_SHARES, adjust_for_splits, build_history, parse_announcement
from valuation_monitor.market_calendar import BR, get_market
from valuation_monitor.providers import cvm
from valuation_monitor.providers.base import Filing
from valuation_monitor.providers.cvm import CvmProvider, delivery_time, parse_listing, read_dataset

ROOT = Path(__file__).parent.parent
FOLDER = ROOT / "data" / "filings" / "TOTS3.SA"
saved = pytest.mark.skipif(not (FOLDER / "index.json").exists(), reason="no saved TOTVS documents (the first scheduled update saves them)")
Q = lambda s: pd.Period(s, freq="Q")  # noqa: E731
COMPANY = "019992"
CNPJ = "53.113.791/0001-22"


# --- synthetic CVM files ---


def csv_bytes(header: list[str], rows: list[list]) -> bytes:
    lines = [";".join(header)] + [";".join(str(v) for v in r) for r in rows]
    return ("\n".join(lines) + "\n").encode("latin-1")


def income_rows(reference: str, version: int, spans: dict[tuple[str, str], dict[str, float]], company: str = COMPANY) -> list[list]:
    return [[COMPANY if company == COMPANY else company, reference, version, start, end, code, value, "REAL", "MIL"]
            for (start, end), codes in spans.items() for code, value in codes.items()]


def dataset_zip(kind: str, year: int, documents: list[list], income: list[list], balance: list[list], shares: list[list]) -> bytes:
    prefix = f"{kind.lower()}_cia_aberta"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(f"{prefix}_{year}.csv", csv_bytes(["CNPJ_CIA", "DT_REFER", "VERSAO", "CD_CVM", "CATEG_DOC", "ID_DOC", "DT_RECEB", "LINK_DOC"], documents))
        z.writestr(f"{prefix}_DRE_con_{year}.csv", csv_bytes(["CD_CVM", "DT_REFER", "VERSAO", "DT_INI_EXERC", "DT_FIM_EXERC", "CD_CONTA", "VL_CONTA", "MOEDA", "ESCALA_MOEDA"], income))
        z.writestr(f"{prefix}_BPP_con_{year}.csv", csv_bytes(["CD_CVM", "DT_REFER", "VERSAO", "DT_FIM_EXERC", "CD_CONTA", "VL_CONTA", "MOEDA", "ESCALA_MOEDA"], balance))
        z.writestr(f"{prefix}_composicao_capital_{year}.csv", csv_bytes(
            ["CNPJ_CIA", "DT_REFER", "VERSAO", "QT_ACAO_ORDIN_CAP_INTEGR", "QT_ACAO_PREF_CAP_INTEGR", "QT_ACAO_ORDIN_TESOURO", "QT_ACAO_PREF_TESOURO"], shares))
    return out.getvalue()


def link(doc_id: int) -> str:
    return f"http://www.rad.cvm.gov.br/ENETCONSULTA/frmDownloadDocumento.aspx?CodigoInstituicao=1&NumeroSequencialDocumento={doc_id}"


ITR_Q2_2025 = {  # thousands of reais; EPS per share
    ("2025-04-01", "2025-06-30"): {"3.01": 1500000.0, "3.11": 195000.0, "3.11.01": 188718.0, "3.99.01.01": 0.32145, "3.99.01.02": 0.0, "3.99.02.01": 0.31525},
    ("2025-01-01", "2025-06-30"): {"3.01": 2900000.0, "3.11.01": 383365.0, "3.99.01.01": 0.65365},
    ("2024-04-01", "2024-06-30"): {"3.01": 1300000.0, "3.11.01": 114872.0, "3.99.01.01": 0.19304},
    ("2024-01-01", "2024-06-30"): {"3.01": 2550000.0, "3.11.01": 241521.0, "3.99.01.01": 0.40318},
}
BALANCE_Q2_2025 = [[COMPANY, "2025-06-30", 1, end, code, value, "REAL", "MIL"] for end, code, value in [
    ("2025-06-30", "2.03", 5215253.0), ("2025-06-30", "2.03.09", 318086.0), ("2024-12-31", "2.03", 4987121.0), ("2024-12-31", "2.03.09", 305769.0),
    ("2025-06-30", "2.01", 999.0),  # another account: not kept
]]


def itr_2025_zip() -> bytes:
    documents = [
        [CNPJ, "2025-06-30", 1, COMPANY, "ITR", 150669, "2025-08-06", link(150669)],
        [CNPJ, "2025-03-31", 1, COMPANY, "ITR", 147118, "2025-05-08", link(147118)],
        [CNPJ, "2025-03-31", 2, COMPANY, "ITR", 149000, "2025-07-01", link(149000)],
        ["11.111.111/0001-11", "2025-06-30", 1, "000001", "ITR", 1, "2025-08-01", link(1)],  # another company
    ]
    q1 = {("2025-01-01", "2025-03-31"): {"3.01": 1400000.0, "3.11.01": 194647.0, "3.99.01.01": 0.33222},
          ("2024-01-01", "2024-03-31"): {"3.01": 1250000.0, "3.11.01": 126649.0, "3.99.01.01": 0.21003}}
    income = income_rows("2025-06-30", 1, ITR_Q2_2025) + income_rows("2025-03-31", 2, q1) + [["000001", "2025-06-30", 1, "2025-04-01", "2025-06-30", "3.01", 5.0, "REAL", "MIL"]]
    balance = BALANCE_Q2_2025 + [[COMPANY, "2025-03-31", 2, end, code, value, "REAL", "MIL"] for end, code, value in [
        ("2025-03-31", "2.03", 5111498.0), ("2025-03-31", "2.03.09", 311717.0), ("2024-12-31", "2.03", 4987121.0), ("2024-12-31", "2.03.09", 305769.0)]]
    shares = [[CNPJ, "2025-06-30", 1, 599401581, 0, 13492877, 0], [CNPJ, "2025-03-31", 2, 599401581, 0, 13492877, 0]]
    return dataset_zip("ITR", 2025, documents, income, balance, shares)


def form_zip(kind: str, delivered: str, extra: dict[str, bytes] | None = None) -> bytes:
    """A delivered document: its form with the delivery time (and an empty date before it, as in the real forms)."""
    form = f"<Documento><Cadastro><DataEntrega>0001-01-01T00:00:00</DataEntrega></Cadastro><DataEntrega>{delivered}.293</DataEntrega></Documento>"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(cvm.FORM_NAMES[kind], form.encode())
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return out.getvalue()


LISTING = """<html><body><pre>
<a href="../">../</a>
<a href="itr_cia_aberta_2011.zip">itr_cia_aberta_2011.zip</a>                            05-Aug-2024 22:24     23M
<a href="itr_cia_aberta_2025.zip">itr_cia_aberta_2025.zip</a>                            22-Nov-2026 08:11     30M
</pre></body></html>"""


def test_parse_listing():
    assert parse_listing(LISTING) == {2011: ("itr_cia_aberta_2011.zip", "05-Aug-2024 22:24"), 2025: ("itr_cia_aberta_2025.zip", "22-Nov-2026 08:11")}


def test_read_dataset_keeps_every_version_and_the_figures_of_the_latest():
    docs = read_dataset(itr_2025_zip(), "ITR", 2025, COMPANY)
    assert [(d["reference_date"], d["version"], d["document_id"]) for d in docs] == [("2025-03-31", 1, 147118), ("2025-03-31", 2, 149000), ("2025-06-30", 1, 150669)]
    assert docs[0]["extract"] is None  # replaced by version 2: the open data has only the figures of the latest version
    assert docs[2]["link"].startswith("https://www.rad.cvm.gov.br/") and docs[2]["received_date"] == "2025-08-06"
    extract = docs[2]["extract"]
    assert (extract["currency"], extract["scale"]) == ("BRL", 1000)
    assert {r["code"] for r in extract["income_statement"]} == {"3.01", "3.11", "3.11.01", "3.99.01.01", "3.99.01.02", "3.99.02.01"}
    assert [r["code"] for r in extract["balance_sheet"]] == ["2.03", "2.03", "2.03.09", "2.03.09"]  # 2.01 is not kept
    assert extract["shares"] == {"ordinary": 599401581, "preferred": 0, "ordinary_treasury": 13492877, "preferred_treasury": 0}
    assert read_dataset(itr_2025_zip(), "ITR", 2025, "999999") == []


def test_delivery_time_skips_the_empty_dates():
    assert delivery_time(form_zip("ITR", "2025-08-06T18:29:20"), "ITR") == pd.Timestamp("2025-08-06 18:29:20")
    with pytest.raises(zipfile.BadZipFile):  # the document service answers an error page with status 200 when it is down
        delivery_time(b"The HTTP service ... is unavailable.", "ITR")


def extract_content(doc: dict, delivered: str = "2025-08-06T18:29:20") -> bytes:
    return json.dumps(cs.make_extract(doc, pd.Timestamp(delivered), "CVM open data", doc["extract"])).encode()


def test_parse_a_second_quarter_itr():
    doc = read_dataset(itr_2025_zip(), "ITR", 2025, COMPANY)[2]
    p = cs.parse_cvm_statements(extract_content(doc))
    assert (p.currency, p.period, p.quarterly) == ("BRL", Q("2025Q2"), True)
    assert p.values["revenue"] == [1500.0, 1300.0]  # the three months of this quarter and of the same quarter last year, millions
    assert p.values["profit"] == [pytest.approx(188.718), pytest.approx(114.872)] and p.values["eps"] == [0.32145, 0.19304]
    assert p.values["equity"] == [pytest.approx(4897.167), pytest.approx(4681.352)]  # total equity less non-controlling interests
    assert p.values["wa_shares"] == [pytest.approx(585.908704)]  # period-end shares outstanding


def test_parse_a_first_quarter_and_a_year():
    doc = read_dataset(itr_2025_zip(), "ITR", 2025, COMPANY)[1]
    p = cs.parse_cvm_statements(extract_content(doc))
    assert (p.period, p.quarterly, p.values["revenue"], p.values["eps"]) == (Q("2025Q1"), True, [1400.0, 1250.0], [0.33222, 0.21003])
    year = {**doc, "kind": "DFP", "reference_date": "2025-12-31", "extract": {**doc["extract"], "income_statement": [
        {"code": c, "start": s, "end": e, "value": v} for c, s, e, v in [
            ("3.01", "2025-01-01", "2025-12-31", 6000000.0), ("3.01", "2024-01-01", "2024-12-31", 5200000.0),
            ("3.11.01", "2025-01-01", "2025-12-31", 900000.0), ("3.11.01", "2024-01-01", "2024-12-31", 700000.0),
            ("3.99.01.01", "2025-01-01", "2025-12-31", 1.5), ("3.99.01.01", "2024-01-01", "2024-12-31", 1.2)]],
        "balance_sheet": [{"code": "2.03", "end": "2025-12-31", "value": 5500000.0}, {"code": "2.03", "end": "2024-12-31", "value": 4987121.0}]}}
    p = cs.parse_cvm_statements(extract_content(year))
    assert (p.period, p.quarterly) == (Q("2025Q4"), False)
    assert p.values["revenue"] == [6000.0, 5200.0] and p.values["eps"] == [1.5, 1.2] and p.values["equity"] == [5500.0, pytest.approx(4987.121)]


def test_refuses_preferred_shares_and_missing_figures():
    doc = read_dataset(itr_2025_zip(), "ITR", 2025, COMPANY)[2]
    with_preferred = {**doc, "extract": {**doc["extract"], "income_statement": doc["extract"]["income_statement"] + [
        {"code": "3.99.01.02", "start": "2025-04-01", "end": "2025-06-30", "value": 0.35}]}}
    with pytest.raises(ValueError, match="ordinary shares only"):
        cs.parse_cvm_statements(extract_content(with_preferred))
    no_revenue = {**doc, "extract": {**doc["extract"], "income_statement": [r for r in doc["extract"]["income_statement"] if r["code"] != "3.01"]}}
    with pytest.raises(ValueError, match="3.01"):
        cs.parse_cvm_statements(extract_content(no_revenue))
    with pytest.raises(ValueError, match="currency"):
        cs.currency_of({"DOLAR"})


def test_english_titles():
    assert cs.english_title("ITR", pd.Timestamp("2025-09-30")) == "ITR 3Q25"
    assert cs.english_title("DFP", pd.Timestamp("2025-12-31")) == "DFP 2025"
    assert cs.english_title("ITR", pd.Timestamp("2020-03-31"), 2) == "ITR 1Q20 (version 2)"


# --- the XML of a delivered document (a version replaced later) ---


def old_layout_document(kind: str, delivered: str = "2020-08-05T18:01:02") -> bytes:
    """The older layout: an inner .itr zip with InfoFinaDFin.xml (numbered columns) and the share capital."""
    def account(statement: str, scope: str, code: str, columns: dict[int, float]) -> str:
        values = "".join(f"<ValorConta{i}>{columns.get(i, 0.0):.10f}</ValorConta{i}>" for i in range(1, 13))
        return (f"<InfoFinaDFin><PlanoConta><VersaoPlanoConta><CodigoTipoDemonstracaoFinanceira>{statement}</CodigoTipoDemonstracaoFinanceira>"
                f"<CodigoTipoInformacaoFinanceira>{scope}</CodigoTipoInformacaoFinanceira></VersaoPlanoConta><NumeroConta>{code}</NumeroConta>"
                f"</PlanoConta>{values}</InfoFinaDFin>")
    accounts = "".join([
        account("4", "2", "3.01", {2: 627399.0, 4: 1228817.0, 5: 564002.0, 6: 1127589.0}),
        account("4", "2", "3.11.01", {2: 57993.0, 4: 119501.0, 5: 57036.0, 6: 69118.0}),
        account("4", "2", "3.99.01.01", {2: 0.1019, 4: 0.21, 5: 0.112, 6: 0.1349}),
        account("4", "1", "3.01", {2: 1.0}),  # the individual statements: not read
        account("3", "2", "2.03", {2: 2517411.0, 3: 2478409.0}),
        account("3", "2", "2.03.09", {3: 1195.0}),
    ])
    capital = ("<Composicao><QuantidadeAcaoOrdinariaCapitalIntegralizado>577913181</QuantidadeAcaoOrdinariaCapitalIntegralizado>"
               "<QuantidadeAcaoPreferencialCapitalIntegralizado>0</QuantidadeAcaoPreferencialCapitalIntegralizado>"
               "<QuantidadeAcaoOrdinariaTesouraria>10158278</QuantidadeAcaoOrdinariaTesouraria>"
               "<QuantidadeAcaoPreferencialTesouraria>0</QuantidadeAcaoPreferencialTesouraria></Composicao>")
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("InfoFinaDFin.xml", f"<ArrayOfInfoFinaDFin>{accounts}</ArrayOfInfoFinaDFin>")
        z.writestr("ComposicaoCapitalSocialDemonstracaoFinanceiraNegocios.xml", capital)
    return form_zip(kind, delivered, {f"019992202006300301.{kind.lower()}": inner.getvalue()})


def new_layout_document() -> bytes:
    def account(code: str, **columns: str) -> str:
        return f"<Conta><CodigoConta>{code}</CodigoConta>" + "".join(f"<{k}>{v}</{k}>" for k, v in columns.items()) + "</Conta>"
    income = "".join([
        account("3.01", TrimestreAtual="1.500.000", AcumuladoExercicioAtual="2.900.000", TrimestreAnterior="1.300.000", AcumuladoExercicioAnterior="2.550.000"),
        account("3.11.01", TrimestreAtual="188718", AcumuladoExercicioAtual="383365", TrimestreAnterior="114872", AcumuladoExercicioAnterior="241521"),
        account("3.99.01.01", TrimestreAtual="0,32145", AcumuladoExercicioAtual="0,65365", TrimestreAnterior="0,19304", AcumuladoExercicioAnterior="0,40318"),
        account("3.99", TrimestreAtual="", AcumuladoExercicioAtual="", TrimestreAnterior="", AcumuladoExercicioAnterior=""),
    ])
    balance = account("2.03", TrimestreAtual="5.215.253", ExercicioAnterior="4.987.121") + account("2.03.09", TrimestreAtual="318.086", ExercicioAnterior="305.769")
    xml = (f"<XmlInformacoesTrimestraisFinanceiras><DadosITR><Moeda>1</Moeda><EscalaMoeda>2</EscalaMoeda><Formulario><DadosEmpresa><ComposicaoCapital>"
           "<CaptalIntegralizado><Ordinarias>599401581</Ordinarias><Preferenciais>0</Preferenciais></CaptalIntegralizado>"
           "<Tesouraria><Ordinarias>13492877</Ordinarias><Preferenciais>0</Preferenciais></Tesouraria></ComposicaoCapital></DadosEmpresa>"
           f"<DfIndividuais><DemonstracaoResultado>{account('3.01', TrimestreAtual='1')}</DemonstracaoResultado></DfIndividuais>"
           f"<DfConsolidadas><BalancoPatrimonialPassivo>{balance}</BalancoPatrimonialPassivo><DemonstracaoResultado>{income}</DemonstracaoResultado>"
           "</DfConsolidadas></Formulario></DadosITR></XmlInformacoesTrimestraisFinanceiras>")
    return form_zip("ITR", "2025-08-06T18:29:20", {"019992ITR30-06-2025v1.xml": xml.encode()})


def test_read_the_older_layout():
    extract = cs.read_document(old_layout_document("ITR"), "ITR", pd.Timestamp("2020-06-30"))
    rows = {(r["code"], r["start"], r["end"]): r["value"] for r in extract["income_statement"]}
    assert rows[("3.01", "2020-04-01", "2020-06-30")] == 627399.0 and rows[("3.01", "2020-01-01", "2020-06-30")] == 1228817.0
    assert rows[("3.99.01.01", "2019-04-01", "2019-06-30")] == 0.112 and rows[("3.11.01", "2019-01-01", "2019-06-30")] == 69118.0
    assert [(r["code"], r["end"], r["value"]) for r in extract["balance_sheet"]] == [
        ("2.03", "2019-12-31", 2478409.0), ("2.03", "2020-06-30", 2517411.0), ("2.03.09", "2019-12-31", 1195.0), ("2.03.09", "2020-06-30", 0.0)]
    assert extract["shares"]["ordinary_treasury"] == 10158278


def test_read_the_newer_layout_gives_what_the_open_data_gives():
    extract = cs.read_document(new_layout_document(), "ITR", pd.Timestamp("2025-06-30"))
    from_csv = read_dataset(itr_2025_zip(), "ITR", 2025, COMPANY)[2]["extract"]
    key = lambda rows: {(r["code"], r.get("start"), r["end"]): r["value"] for r in rows if r["value"] and r["code"] in ("3.01", "3.11.01", "3.99.01.01", "2.03", "2.03.09")}  # noqa: E731
    for part in cs.STATEMENTS:
        assert key(extract[part]) == key(from_csv[part])
    assert extract["shares"] == from_csv["shares"] and (extract["currency"], extract["scale"]) == ("BRL", 1000)


# --- the provider: what is downloaded, partial failure ---


class FakeHttp:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[tuple[str, bool]] = []

    def get_bytes(self, url, params=None, immutable=False, cache=True, retries=3):
        self.calls.append((url, immutable))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def provider_responses(listing_modified: str = "22-Nov-2026 08:11") -> dict[str, object]:
    listing = f'<a href="itr_cia_aberta_2025.zip">itr_cia_aberta_2025.zip</a>     {listing_modified}     30M'
    return {
        cvm.DATASETS["DFP"]: b"<html></html>",
        cvm.DATASETS["ITR"]: listing.encode(),
        cvm.DATASETS["ITR"] + "itr_cia_aberta_2025.zip": itr_2025_zip(),
        link(147118).replace("http://", "https://"): old_layout_document("ITR", "2025-05-08T18:29:20"),  # replaced by version 2: read from its XML
        link(149000).replace("http://", "https://"): form_zip("ITR", "2025-07-01T19:00:00"),
        link(150669).replace("http://", "https://"): form_zip("ITR", "2025-08-06T18:29:20"),
    }


def test_partial_failure_saves_what_was_fetched_and_retries_the_rest(tmp_path, capsys):
    responses = provider_responses()
    responses[link(149000).replace("http://", "https://")] = requests.ConnectionError("document service down")
    http = FakeHttp(responses)
    provider = CvmProvider(http, tmp_path)
    filings = provider.results_announcements("TOTS3.SA", datetime(2024, 1, 1), now=datetime(2026, 11, 27))
    assert "ERROR TOTS3.SA: ITR 2025-03-31 version 2" in capsys.readouterr().err
    index = json.loads((tmp_path / "TOTS3.SA" / "index.json").read_text())
    assert [(e["document_id"], e["published"]) for e in index] == [(147118, "2025-05-08T18:29:20"), (150669, "2025-08-06T18:29:20")]
    assert not (tmp_path / "TOTS3.SA" / "datasets.json").exists()  # not every document was saved: the file is read again next time
    assert [f.title for f in filings] == ["ITR 1Q25", "ITR 2Q25"]

    responses[link(149000).replace("http://", "https://")] = form_zip("ITR", "2025-07-01T19:00:00")
    filings = provider.results_announcements("TOTS3.SA", datetime(2024, 1, 1), now=datetime(2026, 11, 27))
    assert [(f.title, f.correction) for f in filings] == [("ITR 1Q25", False), ("ITR 1Q25 (version 2)", True), ("ITR 2Q25", False)]  # in order of delivery
    assert "restating the one first delivered 2025-05-08 18:29" in filings[1].note
    assert json.loads((tmp_path / "TOTS3.SA" / "datasets.json").read_text()) == {"itr_cia_aberta_2025.zip": "22-Nov-2026 08:11"}

    # nothing new on the listing: the open-data file is not downloaded again; a rebuilt file is, its known documents are not
    http.calls.clear()
    provider.results_announcements("TOTS3.SA", datetime(2024, 1, 1), now=datetime(2026, 11, 27))
    assert [url for url, _ in http.calls] == [cvm.DATASETS["DFP"], cvm.DATASETS["ITR"]]
    http.responses[cvm.DATASETS["ITR"]] = provider_responses("29-Nov-2026 08:11")[cvm.DATASETS["ITR"]]
    http.calls.clear()
    provider.results_announcements("TOTS3.SA", datetime(2024, 1, 1), now=datetime(2026, 11, 27))
    assert [url for url, _ in http.calls] == [cvm.DATASETS["DFP"], cvm.DATASETS["ITR"], cvm.DATASETS["ITR"] + "itr_cia_aberta_2025.zip"]


def test_the_share_capital_missing_from_the_open_data_is_read_from_the_document(tmp_path):
    responses = provider_responses()
    data = itr_2025_zip()
    with zipfile.ZipFile(io.BytesIO(data)) as z:  # the same file without the share capital of the second quarter
        files = {n: z.read(n) for n in z.namelist()}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n, b in files.items():
            z.writestr(n, b"\n".join(line for line in b.split(b"\n") if not (b"composicao" in n.encode() and b"2025-06-30" in line)))
    responses[cvm.DATASETS["ITR"] + "itr_cia_aberta_2025.zip"] = out.getvalue()
    responses[link(150669).replace("http://", "https://")] = new_layout_document()
    CvmProvider(FakeHttp(responses), tmp_path).results_announcements("TOTS3.SA", datetime(2024, 1, 1), now=datetime(2026, 11, 27))
    entry = next(e for e in json.loads((tmp_path / "TOTS3.SA" / "index.json").read_text()) if e["document_id"] == 150669)
    saved = json.loads((tmp_path / "TOTS3.SA" / entry["file"]).read_text())
    assert saved["shares"]["ordinary"] == 599401581 and saved["values_from"] == "CVM open data; the share capital from the delivered document"


def test_closed_years_are_read_once_and_cached_as_immutable(tmp_path):
    http = FakeHttp(provider_responses())
    provider = CvmProvider(http, tmp_path)
    provider.results_announcements("TOTS3.SA", datetime(2024, 1, 1), now=datetime(2028, 1, 10))  # 2025 is closed in 2028
    assert (cvm.DATASETS["ITR"] + "itr_cia_aberta_2025.zip", True) in http.calls
    http.responses[cvm.DATASETS["ITR"]] = provider_responses("29-Nov-2027 08:11")[cvm.DATASETS["ITR"]]
    http.calls.clear()
    provider.results_announcements("TOTS3.SA", datetime(2024, 1, 1), now=datetime(2028, 1, 10))
    assert cvm.DATASETS["ITR"] + "itr_cia_aberta_2025.zip" not in [url for url, _ in http.calls]


def test_without_the_network_the_saved_documents_are_used(tmp_path, capsys):
    CvmProvider(FakeHttp(provider_responses()), tmp_path).results_announcements("TOTS3.SA", datetime(2024, 1, 1), now=datetime(2026, 11, 27))
    offline = FakeHttp({url: requests.ConnectionError("offline") for url in provider_responses()})
    filings = CvmProvider(offline, tmp_path).results_announcements("TOTS3.SA", datetime(2024, 1, 1), now=datetime(2026, 11, 27))
    assert len(filings) == 3 and "using the 3 saved documents" in capsys.readouterr().err
    with pytest.raises(requests.ConnectionError):
        CvmProvider(offline, tmp_path / "empty").results_announcements("TOTS3.SA", datetime(2024, 1, 1), now=datetime(2026, 11, 27))


# --- the market, the configuration and the history ---


def test_market_br_closes_at_17():
    assert get_market("BR") is BR and BR.timezone == "America/Sao_Paulo"
    assert BR.available_date(pd.Timestamp("2025-08-06 18:29")) == pd.Timestamp("2025-08-07")
    assert BR.available_date(pd.Timestamp("2025-11-12 17:30")) == pd.Timestamp("2025-11-13")  # B3 still open until about 18:00 then: a day late, never early
    assert BR.available_date(pd.Timestamp("2025-08-06 09:00")) == pd.Timestamp("2025-08-06")
    assert DEFAULT_MAX_PRICE_AGE_DAYS["BR"] == 6


def test_config_for_a_brazilian_stock():
    config = parse_config({"stocks": [{"symbol": "TOTS3.SA", "name": "TOTVS", "metrics": ["pe", "ps", "pb"]}]})
    stock = config.stocks[0]
    assert (stock.market, stock.filing_format) == ("BR", "cvm_statements")
    with pytest.raises(ValueError, match="does not match market"):
        parse_config({"stocks": [{"symbol": "TOTS3.SA", "name": "TOTVS", "filing_format": "jp_tanshin"}]})


def announcement(doc: dict, delivered: str, correction: bool = False):
    content = extract_content(doc, delivered)
    filing = Filing(title="x", published=pd.Timestamp(delivered), url=f"u{doc['document_id']}", content=content, correction=correction)
    return parse_announcement(filing, "cvm_statements", BR)


def test_a_late_restatement_with_the_same_figures_adds_nothing():
    q1, _, q2 = read_dataset(itr_2025_zip(), "ITR", 2025, COMPANY)
    q1 = {**q1, "extract": read_dataset(itr_2025_zip(), "ITR", 2025, COMPANY)[1]["extract"]}
    first = announcement(q1, "2025-05-08T18:29:20")
    assert (first.shares_label, first.is_quarterly_table) == (PERIOD_END_SHARES, True)
    second = announcement(q2, "2025-08-06T18:29:20")
    restated = announcement({**q1, "document_id": 149000, "version": 2}, "2025-09-01T19:00:00", correction=True)
    plain = build_history(adjust_for_splits([first, second], pd.Series(dtype=float)))
    history = build_history(adjust_for_splits([first, second, restated], pd.Series(dtype=float)))
    assert history.ttm.equals(plain.ttm)

    changed = {**q1, "document_id": 149001, "version": 3, "extract": {**q1["extract"], "income_statement": [
        {**r, "value": r["value"] + 1000.0} if r["code"] == "3.01" else r for r in q1["extract"]["income_statement"]]}}
    with pytest.raises(ValueError, match="after a later quarter"):
        build_history(adjust_for_splits([first, second, announcement(changed, "2025-09-01T19:00:00", correction=True)], pd.Series(dtype=float)))


# --- the saved extracts ---


def saved_index() -> list[dict]:
    return json.loads((FOLDER / "index.json").read_text(encoding="utf-8"))


@saved
def test_every_saved_extract_is_ascii_codes_and_values():
    allowed = {"source", "kind", "company_code", "reference_date", "version", "document_id", "received_date", "delivered", "values_from", "link",
               "currency", "scale", "income_statement", "balance_sheet", "shares"}
    for e in saved_index():
        text = (FOLDER / e["file"]).read_text(encoding="utf-8")
        assert text.isascii(), e["file"]
        data = json.loads(text)
        assert set(data) == allowed, e["file"]
        assert all(set(r) <= {"code", "start", "end", "value"} for part in cs.STATEMENTS for r in data[part])


@saved
def test_every_saved_extract_parses_and_profit_over_eps_agrees_with_the_shares():
    for e in saved_index():
        p = cs.parse_cvm_statements((FOLDER / e["file"]).read_bytes())
        assert str(p.period.end_time.date()) == e["reference_date"]
        implied = p.values["profit"][0] / p.values["eps"][0]
        shares = p.values["wa_shares"][0] if "wa_shares" in p.values else None
        assert shares is not None, e["title"]  # from the open data, or (before 2020) from the delivered document
        if not p.notes:  # 1Q19: the EPS of continuing operations (annotated)
            # weighted average (EPS) vs period-end shares: TOTVS issued 31 million shares in the third quarter of 2021 (6.5% apart then)
            assert implied == pytest.approx(shares, rel=0.07), e["title"]


@saved
def test_the_saved_history_builds():
    config = load_config(ROOT / "config.yaml")
    stock = next(s for s in config.stocks if s.symbol == "TOTS3.SA")
    filings = CvmProvider(None, ROOT / "data" / "filings").results_announcements("TOTS3.SA", datetime(2014, 1, 1))
    parsed = [parse_announcement(f, stock.filing_format, BR) for f in filings]
    splits = pd.Series([5.0, 3.0], index=pd.to_datetime(["2011-03-22", "2020-05-04"]))
    history = build_history(adjust_for_splits(parsed, splits))
    assert history.currency == "BRL" and len(history.ttm) > 40


def test_a_period_with_discontinued_operations_is_annotated():
    """TOTVS's 1Q19 ITR: its basic EPS is that of continuing operations, its net income includes the discontinued hardware business."""
    doc = read_dataset(itr_2025_zip(), "ITR", 2025, COMPANY)[1]
    rows = [r for r in doc["extract"]["income_statement"] if r["code"] not in ("3.01", "3.11.01", "3.99.01.01") or r["start"] == "2024-01-01"]
    rows += [{"code": c, "start": "2025-01-01", "end": "2025-03-31", "value": v} for c, v in [
        ("3.01", 563587.0), ("3.09", 43862.0), ("3.10", -31744.0), ("3.11.01", 12082.0), ("3.11.02", 36.0), ("3.99.01.01", 0.268)]]
    p = cs.parse_cvm_statements(extract_content({**doc, "extract": {**doc["extract"], "income_statement": rows}}))
    assert p.values["eps"][0] == 0.268 and p.values["profit"][0] == pytest.approx(12.082)  # used as disclosed
    assert p.notes == {"eps": "the period has discontinued operations (BRL -31.7 million of the consolidated net income); the basic EPS 0.268 is the disclosed "
                              "figure: net income / EPS = 45.1 million shares, net income from continuing operations less non-controlling interests / EPS = "
                              "163.5 million shares"}
    assert cs.parse_cvm_statements(extract_content(doc)).notes is None
    filing = Filing(title="ITR 1Q19", published=pd.Timestamp("2019-05-08 17:52"), url="u", content=extract_content({**doc, "extract": {**doc["extract"], "income_statement": rows}}))
    assert parse_announcement(filing, "cvm_statements", BR).series_notes == p.notes
