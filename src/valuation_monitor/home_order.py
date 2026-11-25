"""The order of the stocks on the home page and which of them are hidden there.

config.yaml is the only place this is kept: the home page shows the stocks in the order of its `stocks` list and leaves out
those with `hidden: true`. The "Order" panel of the home page saves a new order by starting the workflow
.github/workflows/home-order.yml, which calls `valuation-monitor-home-order` (main() below) to rewrite config.yaml:

  - the request is checked strictly (parse_request): its shape and size, the order is exactly the configured symbols, and the
    hidden ones are among them;
  - whole stock blocks are moved and only a `hidden: true` line is added or removed (rewrite), so every comment and all other
    formatting stay as they were;
  - the result is checked (check_rewrite): parsed, it must equal the old config in everything but the order and `hidden`.

Hidden only concerns the home page: the data of a hidden stock is still updated, and its page, downloads and MCP lookups work.

The GitHub Pages export also publishes `home-order.json` (payload()) next to the pages. The home page fetches the newest copy when
it loads and applies it (apply(); app's home-order.js does the same in the browser), so a page served from an older config.yaml,
e.g. the Cloud Run image, follows a new order without being redeployed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

from valuation_monitor.config import Config, load_config, parse_config

FILE_NAME = "home-order.json"  # published on the GitHub Pages site only; never committed
WORKFLOW = "home-order.yml"  # the workflow the "Order" panel starts
BRANCH = "main"  # the branch it runs on and commits to
MAX_REQUEST_BYTES = 16 * 1024  # a request is a few hundred bytes; anything far larger is not from the panel


def payload(config: Config) -> dict:
    """What home-order.json holds: every configured symbol in the configured order, and the hidden ones."""
    return {"order": [s.symbol for s in config.stocks], "hidden": [s.symbol for s in config.stocks if s.hidden]}


def apply(symbols: list[str], hidden: set[str] | list[str], published) -> tuple[list[str], list[str]]:
    """The order and hidden stocks a page shows, from its own config (symbols in order, hidden) and a published home-order.json.

    The published file wins for every stock it lists. A page's stock that the file does not list (the page's config is newer)
    keeps its own hidden flag and comes after the listed ones in its own order; a listed symbol the page does not have is
    ignored. Anything that is not a well-formed file leaves the page as it is. home-order.js mirrors this rule exactly.
    """
    hidden = set(hidden)
    if not (isinstance(published, dict) and isinstance(published.get("order"), list) and isinstance(published.get("hidden"), list)):
        return list(symbols), [s for s in symbols if s in hidden]
    known = set(symbols)
    listed = list(dict.fromkeys(s for s in published["order"] if isinstance(s, str) and s in known))
    order = listed + [s for s in symbols if s not in set(listed)]
    published_hidden = {s for s in published["hidden"] if isinstance(s, str)}
    shown_hidden = {s for s in listed if s in published_hidden} | {s for s in symbols if s not in set(listed) and s in hidden}
    return order, [s for s in order if s in shown_hidden]


def parse_request(text: str, config: Config) -> tuple[list[str], list[str]]:
    """Check a request of the "Order" panel strictly: {"order": [every configured symbol once], "hidden": [some of them]}."""
    if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError(f"The request is larger than {MAX_REQUEST_BYTES} bytes")
    try:
        request = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"The request is not valid JSON: {e}") from None
    if not isinstance(request, dict) or set(request) != {"order", "hidden"}:
        raise ValueError('The request must be a JSON object with exactly the keys "order" and "hidden"')
    configured = [s.symbol for s in config.stocks]
    for key in ("order", "hidden"):
        items = request[key]
        if not isinstance(items, list) or not all(isinstance(s, str) for s in items):
            raise ValueError(f'"{key}" must be a list of symbols')
        duplicates = sorted({s for s in items if items.count(s) > 1})
        if duplicates:
            raise ValueError(f'"{key}" lists a symbol more than once: {duplicates}')
        unknown = [s for s in items if s not in configured]
        if unknown:
            raise ValueError(f'"{key}" has symbols that are not in config.yaml: {unknown}')
    missing = [s for s in configured if s not in request["order"]]
    if missing:
        raise ValueError(f'"order" must list every stock of config.yaml; missing: {missing}')
    return request["order"], request["hidden"]


STOCKS_KEY = re.compile(r"stocks:[ \t]*(#.*)?\n?")
STOCK_START = re.compile(r"(?P<dash>[ ]*)- (?P<pad>[ ]*)symbol:[ \t]*(?P<symbol>[^\s#]+)")
HIDDEN_LINE = re.compile(r"(?P<indent>[ ]*)hidden:(?P<value>[^\n]*)\n?")


def _blocks(lines: list[str]) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Find the stock entries: (start of the stocks section, its end, [(first line, end of the entry, end of the gap after it)]).

    An entry runs from its "- symbol:" line to its last indented line; the blank lines and column-0 comments after it are the
    gap before the next entry, which stays where it is when entries move.
    """
    try:
        head = next(i for i, line in enumerate(lines) if STOCKS_KEY.fullmatch(line))
    except StopIteration:
        raise ValueError("config.yaml has no top-level stocks: list") from None
    end = next((i for i in range(head + 1, len(lines)) if lines[i].strip() and not lines[i][0].isspace() and not lines[i].startswith(("#", "- "))), len(lines))
    starts = [i for i in range(head + 1, end) if STOCK_START.match(lines[i])]
    if not starts:
        raise ValueError('The stocks of config.yaml must be written as "- symbol: ..." entries')
    stray = [i for i in range(head + 1, starts[0]) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
    dash = {STOCK_START.match(lines[i])["dash"] for i in starts}
    if stray or len(dash) != 1:
        raise ValueError("The stocks list of config.yaml has a layout the order rewrite does not know")
    blocks = []
    for n, first in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else end
        last = max(i for i in range(first, stop) if i == first or (lines[i].strip() and lines[i][0].isspace()))
        blocks.append((first, last + 1, stop))
    return head, end, blocks


def _set_hidden(block: list[str], hidden: bool) -> list[str]:
    """Add or remove the entry's `hidden: true` line; nothing else in the entry changes."""
    start = STOCK_START.match(block[0])
    indent = len(start["dash"]) + 2 + len(start["pad"])  # the column of the entry's keys
    lines = [(i, m) for i, line in enumerate(block) if (m := HIDDEN_LINE.fullmatch(line)) and len(m["indent"]) == indent]
    if len(lines) > 1:
        raise ValueError(f"{start['symbol']}: hidden is written more than once")
    current = bool(lines) and yaml.safe_load(lines[0][1]["value"].split("#")[0].strip() or "null") is True
    if current == hidden:
        return block
    if not hidden:
        return [line for i, line in enumerate(block) if i != lines[0][0]]
    new = " " * indent + "hidden: true\n"
    if lines:  # e.g. "hidden: false"
        return [new if i == lines[0][0] else line for i, line in enumerate(block)]
    name = next((i for i, line in enumerate(block) if i > 0 and line.startswith(" " * indent + "name:")), 0)
    return [*block[: name + 1], new, *block[name + 1 :]]


def rewrite(text: str, order: list[str], hidden: list[str]) -> str:
    """config.yaml with its stock entries in `order` and `hidden: true` on exactly the `hidden` ones."""
    newline_added = not text.endswith("\n")
    lines = (text + "\n" if newline_added else text).splitlines(keepends=True)
    head, end, blocks = _blocks(lines)
    entries = {STOCK_START.match(lines[first])["symbol"]: lines[first:last] for first, last, _ in blocks}
    if sorted(entries) != sorted(order) or len(entries) != len(blocks):
        raise ValueError("The order does not list the stocks of config.yaml exactly once each")
    out = lines[: blocks[0][0]]
    for (_, last, stop), symbol in zip(blocks, order):
        out += _set_hidden(entries[symbol], symbol in hidden)
        out += lines[last:stop]  # the gap after this position
    out += lines[end:]
    result = "".join(out)
    return result[:-1] if newline_added else result


def _without_hidden(raw: dict) -> dict:
    stocks = sorted(({k: v for k, v in s.items() if k != "hidden"} for s in raw["stocks"]), key=lambda s: str(s["symbol"]))
    return {**raw, "stocks": stocks}


def check_rewrite(old: str, new: str, order: list[str], hidden: list[str]) -> Config:
    """Fail unless the new config.yaml differs from the old one in nothing but the order and the hidden stocks."""
    old_raw, new_raw = yaml.safe_load(old), yaml.safe_load(new)
    if _without_hidden(old_raw) != _without_hidden(new_raw):
        raise ValueError("The rewritten config.yaml differs in more than the order and the hidden stocks")
    kept = Counter(line for line in old.splitlines() if not HIDDEN_LINE.fullmatch(line.strip() + "\n"))
    if kept != Counter(line for line in new.splitlines() if not HIDDEN_LINE.fullmatch(line.strip() + "\n")):
        raise ValueError("The rewritten config.yaml changed lines other than hidden")
    config = parse_config(new_raw)
    if [s.symbol for s in config.stocks] != order or [s.symbol for s in config.stocks if s.hidden] != [s for s in order if s in hidden]:
        raise ValueError("The rewritten config.yaml does not have the requested order and hidden stocks")
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="valuation-monitor-home-order",
        description='Set the home page order and hidden stocks in config.yaml from a request {"order": [...], "hidden": [...]}',
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="The config file to rewrite in place (default: config.yaml)")
    parser.add_argument("--input", default="-", help="File with the JSON request (default: - for standard input)")
    args = parser.parse_args(argv)

    path = Path(args.config)
    try:
        request = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
        old = path.read_text(encoding="utf-8")
        order, hidden = parse_request(request, load_config(path))
        new = rewrite(old, order, hidden)
        check_rewrite(old, new, order, hidden)
    except (ValueError, OSError, yaml.YAMLError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if new == old:
        print(f"{path}: the order and the hidden stocks are already as requested")
        return 0
    path.write_text(new, encoding="utf-8")
    print(f"{path}: order {', '.join(order)}; hidden: {', '.join(hidden) or 'none'}")
    return 0
