"""The home page order and hidden stocks: kept in config.yaml, rewritten by valuation-monitor-home-order (workflow
home-order.yml), published as home-order.json and applied by every home page (home_order.apply, mirrored in home-order.js)."""

import json
import random
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from valuation_monitor import home_order
from valuation_monitor.config import load_config, parse_config
from valuation_monitor.home_order import apply, check_rewrite, main, parse_request, payload, rewrite
from valuation_monitor.service import Monitor

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
SCRIPT = ROOT / "src" / "valuation_monitor" / "web" / "static" / "home-order.js"

# (page symbols, page hidden, published file, expected order, expected hidden): the rule both Python and JavaScript follow
APPLY_CASES = [
    (["A", "B", "C"], [], {"order": ["C", "A", "B"], "hidden": ["A"]}, ["C", "A", "B"], ["A"]),
    (["A", "B", "C"], ["B"], {"order": ["C", "B", "A"], "hidden": []}, ["C", "B", "A"], []),  # the file wins for listed stocks
    (["A", "B", "C"], ["C"], {"order": ["B", "A"], "hidden": ["A"]}, ["B", "A", "C"], ["A", "C"]),  # C is newer than the file
    (["A", "B"], [], {"order": ["X", "B", "A"], "hidden": ["X"]}, ["B", "A"], []),  # X is not on this page
    (["A", "B"], [], {"order": ["B", "B", "A"], "hidden": []}, ["B", "A"], []),  # a repeated symbol counts once
    (["A", "B"], ["A"], None, ["A", "B"], ["A"]),  # no file: the page keeps its own order
    (["A", "B"], ["A"], {"order": "B,A", "hidden": []}, ["A", "B"], ["A"]),  # not a valid file
    (["A", "B"], [], {"order": ["B", "A"]}, ["A", "B"], []),  # no hidden list: not a valid file
    (["A", "B"], [], [1, 2], ["A", "B"], []),
    (["A", "B"], [], {"order": [1, "B", None], "hidden": [2, "B"]}, ["B", "A"], ["B"]),  # non-text entries are ignored
]


@pytest.mark.parametrize("symbols,hidden,published,order,shown_hidden", APPLY_CASES)
def test_apply_published_order(symbols, hidden, published, order, shown_hidden):
    assert apply(symbols, hidden, published) == (order, shown_hidden)


def test_the_browser_applies_the_same_rule():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    cases = [[c[0], c[1], c[2]] for c in APPLY_CASES]
    program = (
        f"const {{ applyOrder }} = require({json.dumps(str(SCRIPT))});"
        f"const cases = {json.dumps(cases)};"
        "process.stdout.write(JSON.stringify(cases.map((c) => applyOrder(c[0], c[1], c[2]))));"
    )
    out = subprocess.run([node, "-e", program], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [[c[3], c[4]] for c in APPLY_CASES]


def test_payload_lists_every_stock_in_config_order():
    config = parse_config({"stocks": [{"symbol": "JD", "name": "JD.com", "market": "US", "cik": 1, "hidden": True},
                                      {"symbol": "0700.HK", "name": "Tencent Holdings"}]})
    assert payload(config) == {"order": ["JD", "0700.HK"], "hidden": ["JD"]}


# config.yaml: hidden and github_repository

def test_hidden_defaults_to_false_and_must_be_a_boolean():
    stock = {"symbol": "0700.HK", "name": "Tencent Holdings"}
    assert parse_config({"stocks": [stock]}).stocks[0].hidden is False
    assert parse_config({"stocks": [{**stock, "hidden": True}]}).stocks[0].hidden is True
    with pytest.raises(ValueError, match="hidden must be true or false"):
        parse_config({"stocks": [{**stock, "hidden": "yes please"}]})


def test_a_stock_may_appear_only_once():
    stock = {"symbol": "0700.HK", "name": "Tencent Holdings"}
    with pytest.raises(ValueError, match="only once"):
        parse_config({"stocks": [stock, stock]})


def test_github_repository():
    stock = {"symbol": "0700.HK", "name": "Tencent Holdings"}
    assert parse_config({"stocks": [stock]}).pages_url is None
    config = parse_config({"stocks": [stock], "github_repository": "Yun-Zhsh/Watchlist-monitor"})
    assert config.pages_url == "https://yun-zhsh.github.io/Watchlist-monitor/"  # GitHub Pages hosts are lower case
    for bad in ("Watchlist-monitor", "a/b/c", "https://github.com/a/b", 5):
        with pytest.raises(ValueError, match="owner/name"):
            parse_config({"stocks": [stock], "github_repository": bad})


def test_the_configured_repository_is_this_one():
    assert real_config().github_repository == "yun-zhsh/Watchlist-monitor"


# The request of the "Order" panel

def real_config():
    return load_config(CONFIG)


def request(order, hidden=()):
    return json.dumps({"order": list(order), "hidden": list(hidden)})


def test_a_valid_request():
    config = real_config()
    symbols = [s.symbol for s in config.stocks]
    order = symbols[::-1]
    assert parse_request(request(order, [symbols[0]]), config) == (order, [symbols[0]])


@pytest.mark.parametrize(
    "text,error",
    [
        ("not json", "not valid JSON"),
        ("[]", "exactly the keys"),
        ('{"order": []}', "exactly the keys"),
        ('{"order": [], "hidden": [], "extra": 1}', "exactly the keys"),
        ('{"order": "META", "hidden": []}', "list of symbols"),
        ('{"order": [1], "hidden": []}', "list of symbols"),
        ("DUPLICATE", "more than once"),
        ("UNKNOWN", "not in config.yaml"),
        ("MISSING", "missing"),
        ("HIDDEN_UNKNOWN", "not in config.yaml"),
        ("HIDDEN_TWICE", "more than once"),
        ("LARGE", "larger than"),
    ],
)
def test_invalid_requests_are_refused(text, error):
    config = real_config()
    symbols = [s.symbol for s in config.stocks]
    text = {
        "DUPLICATE": request(symbols + [symbols[0]]),
        "UNKNOWN": request(symbols + ["NOPE"]),
        "MISSING": request(symbols[1:]),
        "HIDDEN_UNKNOWN": request(symbols, ["NOPE"]),
        "HIDDEN_TWICE": request(symbols, [symbols[0], symbols[0]]),
        "LARGE": request(symbols, []) + " " * home_order.MAX_REQUEST_BYTES,
    }.get(text, text)
    with pytest.raises(ValueError, match=error):
        parse_request(text, config)


# Rewriting config.yaml

def current_state():
    """The order and the hidden stocks of the real config.yaml as it is now: the maintainer changes both from the home page at any
    time, so no test may assume a particular order or that no stock is hidden."""
    config = real_config()
    return [s.symbol for s in config.stocks], [s.symbol for s in config.stocks if s.hidden]


def hidden_lines(text):
    return [line for line in text.splitlines() if line.strip().startswith("hidden:")]


def other_lines(text):
    return sorted(line for line in text.splitlines() if not line.strip().startswith("hidden:"))


def entry_line(lines, symbol):
    return next(i for i, line in enumerate(lines) if (m := home_order.STOCK_START.match(line)) and m["symbol"] == symbol)


def test_rewrite_moves_whole_entries_and_only_adds_hidden_lines():
    old = CONFIG.read_text(encoding="utf-8")
    symbols, hidden_now = current_state()
    shown_now = [s for s in symbols if s not in hidden_now]
    assert len(shown_now) >= 2
    # reverse the order, hide two stocks that are shown now and show again every stock that is hidden now
    order, hidden = symbols[::-1], [shown_now[-1], shown_now[0]]
    new = rewrite(old, order, hidden)
    config = check_rewrite(old, new, order, hidden)
    assert [s.symbol for s in config.stocks] == order
    assert {s.symbol for s in config.stocks if s.hidden} == set(hidden)
    assert hidden_lines(new) == ["    hidden: true"] * 2
    assert other_lines(old) == other_lines(new)  # nothing else changed
    head = old[: old.index("\nstocks:")]
    assert new.startswith(head)  # everything before the list stays in place
    lines = new.splitlines()
    for symbol in hidden:  # the hidden line goes right after the name
        first = entry_line(lines, symbol)
        assert lines[first + 1].startswith("    name:") and lines[first + 2] == "    hidden: true"


def test_rewrite_back_restores_the_file_byte_for_byte():
    old = CONFIG.read_text(encoding="utf-8")
    symbols, hidden_now = current_state()
    shuffled = rewrite(old, symbols[1::2] + symbols[::2], [s for s in symbols if s not in hidden_now][:4])
    assert shuffled != old
    assert rewrite(shuffled, symbols, hidden_now) == old


@pytest.mark.parametrize("seed", range(8))
def test_any_order_and_hidden_state_of_the_real_config(seed):
    """Random orders and hidden sets of the real config.yaml: each one rewrites, passes the check, can be rewritten again,
    and leads back to the file as it is now, byte for byte."""
    rng = random.Random(seed)
    old = CONFIG.read_text(encoding="utf-8")
    symbols, hidden_now = current_state()

    def random_state():
        order = rng.sample(symbols, len(symbols))
        hidden = set(rng.sample(symbols, rng.randint(0, len(symbols))))
        return order, [s for s in order if s in hidden]

    order, hidden = random_state()
    state = rewrite(old, order, hidden)
    config = check_rewrite(old, state, order, hidden)
    assert payload(config) == {"order": order, "hidden": hidden}
    assert parse_request(json.dumps(payload(config)), config) == (order, hidden)
    order2, hidden2 = random_state()
    other = rewrite(state, order2, hidden2)
    check_rewrite(state, other, order2, hidden2)
    assert other_lines(other) == other_lines(old) and len(hidden_lines(other)) == len(hidden2)
    assert rewrite(other, order, hidden) == state
    assert rewrite(state, symbols, hidden_now) == old


SAMPLE = """# top comment
lookback_years: 10
stocks:
  # the first entry
  - symbol: AAA
    name: First   # comment
    market: US
    cik: 1

# --- Hong Kong ---
  - symbol: 0700.HK
    name: Tencent Holdings
    hidden: false
  - symbol: 2382.HK
    name: Sunny Optical
    hidden: true   # for now
levels:
  - { upper: 100, label: All }
"""


def test_rewrite_keeps_gaps_and_column_zero_comments_in_place():
    new = rewrite(SAMPLE, ["2382.HK", "AAA", "0700.HK"], [])
    assert new == """# top comment
lookback_years: 10
stocks:
  # the first entry
  - symbol: 2382.HK
    name: Sunny Optical

# --- Hong Kong ---
  - symbol: AAA
    name: First   # comment
    market: US
    cik: 1
  - symbol: 0700.HK
    name: Tencent Holdings
    hidden: false
levels:
  - { upper: 100, label: All }
"""
    check_rewrite(SAMPLE, new, ["2382.HK", "AAA", "0700.HK"], [])


def test_rewrite_replaces_hidden_false_and_keeps_an_existing_true():
    new = rewrite(SAMPLE, ["AAA", "0700.HK", "2382.HK"], ["0700.HK", "2382.HK"])
    assert "    hidden: true\n  - symbol: 2382.HK" in new and "hidden: false" not in new
    assert "    hidden: true   # for now\n" in new  # untouched
    assert rewrite(SAMPLE, ["AAA", "0700.HK", "2382.HK"], ["2382.HK"]) == SAMPLE


def test_rewrite_of_a_file_without_a_final_newline():
    text = "stocks:\n  - symbol: A\n    name: A\n  - symbol: B\n    name: B"
    assert rewrite(text, ["B", "A"], []) == "stocks:\n  - symbol: B\n    name: B\n  - symbol: A\n    name: A"


@pytest.mark.parametrize(
    "text",
    [
        "lookback_years: 10\n",  # no stocks
        "stocks: [{symbol: A}]\n",  # flow style
        "stocks:\n  - name: A\n    symbol: A\n",  # the symbol is not the first key
        "stocks:\n  - symbol: A\n  - symbol: A\n",  # twice
    ],
)
def test_rewrite_refuses_layouts_it_does_not_know(text):
    with pytest.raises(ValueError):
        rewrite(text, ["A"], [])


def test_check_rewrite_refuses_any_other_change():
    old = CONFIG.read_text(encoding="utf-8")
    symbols = [s.symbol for s in real_config().stocks]
    with pytest.raises(ValueError, match="more than the order"):
        check_rewrite(old, old.replace("metrics: [pe, ps, pb]", "metrics: [pe, pb]", 1), symbols, [])
    with pytest.raises(ValueError, match="more than the order"):
        check_rewrite(old, old.replace("lookback_years: 10", "lookback_years: 5", 1), symbols, [])
    with pytest.raises(ValueError, match="other than hidden"):
        check_rewrite(old, old.replace("# Valuation monitor configuration", "# Changed", 1), symbols, [])
    with pytest.raises(ValueError, match="requested order"):
        check_rewrite(old, old, symbols[::-1], [])


def run_cli(tmp_path, capsys, text, config_text=None):
    path = tmp_path / "config.yaml"
    path.write_text(config_text if config_text is not None else CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "request.json").write_text(text, encoding="utf-8")
    code = main(["--config", str(path), "--input", str(tmp_path / "request.json")])
    return code, path.read_text(encoding="utf-8"), capsys.readouterr()


def test_cli_rewrites_config(tmp_path, capsys):
    symbols = [s.symbol for s in real_config().stocks]
    code, text, out = run_cli(tmp_path, capsys, request(symbols[::-1], [symbols[1]]))
    assert code == 0 and "hidden: " + symbols[1] in out.out
    config = load_config(tmp_path / "config.yaml")
    assert [s.symbol for s in config.stocks] == symbols[::-1]
    code, again, out = run_cli(tmp_path, capsys, request(symbols[::-1], [symbols[1]]), text)
    assert code == 0 and again == text and "already as requested" in out.out


def test_cli_changes_nothing_on_a_bad_request(tmp_path, capsys):
    code, text, out = run_cli(tmp_path, capsys, '{"order": ["META"], "hidden": []}')
    assert code == 1 and "Error:" in out.err and "missing" in out.err
    assert text == CONFIG.read_text(encoding="utf-8")


# Only order and hidden changed: nothing else may notice

def test_order_and_hidden_change_nothing_but_order_and_hidden():
    old = real_config()
    symbols = [s.symbol for s in old.stocks]
    new_text = rewrite(CONFIG.read_text(encoding="utf-8"), symbols[::-1], symbols[:3])
    new = parse_config(yaml.safe_load(new_text))
    assert [s.symbol for s in new.stocks if s.hidden] == symbols[:3][::-1]
    by_symbol = {s.symbol: s for s in new.stocks}
    assert all(replace(by_symbol[s.symbol], hidden=s.hidden) == s for s in old.stocks)  # whatever was hidden before
    assert replace(new, stocks=[]) == replace(old, stocks=[])  # lookbacks, levels, freshness, repository


def dump(path):
    import sqlite3

    with sqlite3.connect(path) as conn:
        return list(conn.iterdump())


def test_saved_data_gives_the_same_results_in_any_order(tmp_path):
    """The real saved data, analyzed offline under the configured order and then under a reversed order with hidden stocks: the
    same results per stock, and the second run writes nothing the first did not (no download or rewrite is keyed on the order)."""
    db = tmp_path / "valuation.db"
    shutil.copyfile(ROOT / "data" / "valuation.db", db)
    config = real_config()
    symbols = [s.symbol for s in config.stocks]
    reordered = parse_config(yaml.safe_load(rewrite(CONFIG.read_text(encoding="utf-8"), symbols[::-1], symbols[::2])))

    def results(cfg):
        monitor = Monitor(cfg, ROOT / "data", tmp_path / "cache", offline=True, db_path=db)
        out = {}
        for report in monitor.analyze_all():
            out[report.stock.symbol] = (
                report.error,
                {r.metric.key: (r.error, {y: w.valuation for y, w in r.windows.items()}) for r in report.metrics},
            )
        return out

    first = results(config)
    saved = dump(db)
    second = results(reordered)
    assert list(second) == symbols[::-1]
    assert first == second
    assert dump(db) == saved
