"""Strings read from the tools' logs reach the page as text, never as markup.

Model names, timestamps and tool names come straight out of the coding tools'
local logs, and several renderers build their rows as HTML strings. Any one of
those values interpolated without escapeHtml is stored XSS: a model named
``"><img src=x onerror=...>`` runs script in the dashboard.

These tests feed values like that through the real renderers, sliced out of
index.html and run in Node over a stub DOM that records every innerHTML write.
Python's HTML parser then reads what was written, the way a browser would
tokenise it. Each value must come back as exactly the text (or attribute value)
it started as, and no element or event-handler attribute may appear that the
renderer did not write itself.

A check on the source only sees the escape calls someone spelled out. Feeding
the value through the renderer sees every place it lands, including a sink
added later. That is also why there is no separate "no bare timestamp
interpolation" sweep: the hostile timestamp reaches every interpolation of it
in each renderer below, whatever the variable is called.
"""
from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest

from support.frontend import extract_js_function, index_source, needs_node, run_node

pytestmark = needs_node


def payload(field: str) -> str:
    """A value that becomes live markup unless it is escaped.

    The quote and bracket close a double-quoted attribute and its tag before the
    ``<img>`` opens, so the same value breaks out of ``title="..."`` and out of a
    text node alike. The trailing ``'&amp;`` has to survive as written too: a
    renderer that escapes twice, or not at all, shows something else.
    """
    return f"{field}\"><img src=x onerror=alert(1)>'&amp;"


MODEL = payload("gpt-5")
TIMESTAMP = payload("2026-10-25T09:00:00Z")
DATE = payload("2026-10-25")
TOOL = payload("bash")

RENDERERS = (
    "function escapeHtml(",
    "function formatToolName(",
    "function hitRateFrom(",
    "function hasModelTokenUsage(",
    "function renderSessionChat(",
    "function renderDrawerMessages(",
    "function renderDrawerTools(",
    "function updateAppsBreakdown(",
    "function updateCombinedModelsTable(",
    "function showDayDetails(",
)

HARNESS = r"""
// --- a DOM just deep enough for the renderers ---------------------------------
// Every innerHTML and insertAdjacentHTML write is recorded: that string is what
// the browser would parse, so it is what the test parses.
const markupWrites = [];

function makeNode(tag, id) {
  let html = '';
  const classes = new Set();
  const node = {
    tagName: String(tag || 'div').toUpperCase(),
    id: id || '',
    className: '',
    title: '',
    textContent: '',
    style: {},
    dataset: {},
    attributes: {},
    children: [],
    classList: {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
    },
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = String(value);
      node.children = [];
      markupWrites.push(html);
    },
    insertAdjacentHTML(_position, value) { markupWrites.push(String(value)); },
    setAttribute(name, value) { node.attributes[name] = String(value); },
    getAttribute(name) { return name in node.attributes ? node.attributes[name] : null; },
    appendChild(child) { node.children.push(child); return child; },
    append(...kids) { node.children.push(...kids); },
    replaceChildren(...kids) { node.children = kids; },
    // The apps breakdown writes its table head as markup, then fills the
    // <tbody> that markup created.
    querySelector(selector) {
      if (selector !== 'tbody') return null;
      if (!node.tbody) node.tbody = makeNode('tbody');
      return node.tbody;
    },
    querySelectorAll() { return []; },
    closest() { return null; },
    addEventListener() {},
    scrollIntoView() {},
  };
  return node;
}

const nodesById = {};
const document = {
  getElementById: (id) => nodesById[id] || (nodesById[id] = makeNode('div', id)),
  createElement: (tag) => makeNode(tag),
  createDocumentFragment: () => makeNode('#document-fragment'),
  querySelectorAll: () => [],
};

// --- page stand-ins: none of them handles the values under test ---------------
function t(key) { return key; }
function formatNumber(value) { return String(Number(value || 0)); }
function formatTokenCount(value) { return String(Number(value || 0)); }
function formatCurrency(value) { return '$' + Number(value || 0).toFixed(2); }
function formatHitRate(rate) {
  return rate === null || rate === undefined ? 'n/a' : (Number(rate) * 100).toFixed(1) + '%';
}
function getBreakdownComparator() { return () => 0; }
function setupBreakdownSort() {}
// The real identity is built with DOM calls, not markup.
function createToolIdentity(tool) {
  const el = makeNode('span');
  el.textContent = String(tool);
  return el;
}
const breakdownSortState = {
  apps: { field: 'cost', direction: 'desc' },
  combined: { field: 'cost', direction: 'desc' },
};
let lastAppsBreakdown = null;
let lastCombinedModels = [];
const MAX_COMBINED_MODELS_ROWS = 300;
let combinedModelsShowAll = false;
let sessionChatPayload = null;
let activeDayDetail = null;

// --- the code under test ------------------------------------------------------
__FUNCTIONS__

// --- driving ------------------------------------------------------------------
const P = __PAYLOADS__;

function markupOf(render) {
  markupWrites.length = 0;
  render();
  return markupWrites.join('\n');
}

const usage = (name) => ({
  name, tokens: 1200, tokens_in: 800, tokens_out: 400, tokens_cache: 0,
  cost: 1.5, messages: 3, cache_hit_rate: 0,
});
const tokens = { input: 800, output: 400, cacheRead: 0, reasoning: 0 };

const out = {
  sessionChat: markupOf(() => renderSessionChat(
    [{ role: 'assistant', model: P.model, timestamp: P.timestamp, content_chars: 120 }],
    [],
  )),
  drawerMessages: markupOf(() => renderDrawerMessages(
    [{ role: 'user', timestamp: P.timestamp, content_chars: 120 }],
  )),
  drawerTools: markupOf(() => renderDrawerTools(
    [{ tool_name: P.tool, timestamp: P.timestamp, has_args: true }],
  )),
  appsBreakdown: markupOf(() => updateAppsBreakdown({ codex: { models: [usage(P.model)] } }, null)),
  combinedModels: markupOf(() => updateCombinedModelsTable([usage(P.model)])),
  dayDetails: markupOf(() => showDayDetails({
    date: P.date,
    totals: { cost: 1.5, tokens: 1200, messages: 3 },
    tokenBreakdown: tokens,
    sources: [{ source: 'codex', modelId: P.model, cost: 1.5, messages: 3, tokens }],
  }, { scroll: false })),
};
process.stdout.write(JSON.stringify(out));
"""


class _Markup(HTMLParser):
    """The elements a browser would build from one innerHTML string.

    Each element keeps its tag, its decoded attributes and the decoded text it
    contains, which is all these tests ask of it.
    """

    VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[dict] = []
        self._open: list[dict] = []

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "text": ""}
        self.elements.append(element)
        if tag not in self.VOID:
            self._open.append(element)

    def handle_startendtag(self, tag, attrs):
        self.elements.append({"tag": tag, "attrs": dict(attrs), "text": ""})

    def handle_endtag(self, tag):
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index]["tag"] == tag:
                del self._open[index:]
                return

    def handle_data(self, data):
        for element in self._open:
            element["text"] += data


def parse(html: str) -> list[dict]:
    parser = _Markup()
    parser.feed(html)
    parser.close()
    return parser.elements


def injected(elements: list[dict]) -> list[dict]:
    """Elements and handlers no renderer here writes, so the payload made them."""
    return [
        element for element in elements
        if element["tag"] == "img" or any(name.startswith("on") for name in element["attrs"])
    ]


def assert_inert(elements: list[dict], *, text: tuple[str, ...] = (), title: tuple[str, ...] = ()) -> None:
    stray = injected(elements)
    assert not stray, f"a log value became markup: {stray}"
    shown = [element["text"].strip() for element in elements]
    for value in text:
        assert value in shown, f"{value!r} is not on the page as its own text; element texts: {shown}"
    titles = [element["attrs"].get("title") for element in elements]
    for value in title:
        assert value in titles, f"{value!r} is not a title attribute as written; titles: {titles}"


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict[str, list[dict]]:
    source = index_source()
    functions = "\n\n".join(extract_js_function(source, signature) for signature in RENDERERS)
    payloads = json.dumps({"model": MODEL, "timestamp": TIMESTAMP, "date": DATE, "tool": TOOL})
    script = tmp_path_factory.mktemp("html-escaping") / "render.js"
    script.write_text(
        HARNESS.replace("__PAYLOADS__", payloads).replace("__FUNCTIONS__", functions),
        encoding="utf-8",
    )
    return {name: parse(html) for name, html in json.loads(run_node(script)).items()}


def test_the_payload_is_live_markup_when_left_unescaped():
    """Without this, a parser that stopped seeing the injection would pass everything."""
    assert injected(parse(f"<td>{MODEL}</td>"))
    assert injected(parse(f'<td title="{MODEL}">gpt-5</td>'))


def test_session_chat_prints_a_hostile_model_and_timestamp_as_text(rendered):
    assert_inert(rendered["sessionChat"], text=(MODEL, TIMESTAMP))


def test_drawer_timeline_prints_a_hostile_timestamp_as_text(rendered):
    assert_inert(rendered["drawerMessages"], text=(TIMESTAMP,))


def test_drawer_tool_list_prints_a_hostile_tool_name_and_timestamp_as_text(rendered):
    assert_inert(rendered["drawerTools"], text=(TOOL, TIMESTAMP))


def test_apps_breakdown_prints_a_hostile_model_name_as_text_and_title(rendered):
    # The model cell carries the name twice: as its text and in title="..." for
    # names too long for the column. The attribute is the context where a bare
    # quote, not a bracket, is what breaks out.
    assert_inert(rendered["appsBreakdown"], text=(MODEL,), title=(MODEL,))


def test_combined_models_table_prints_a_hostile_model_name_as_text(rendered):
    assert_inert(rendered["combinedModels"], text=(MODEL,))


def test_day_details_print_a_hostile_model_and_date_as_text(rendered):
    assert_inert(rendered["dayDetails"], text=(MODEL, DATE))
