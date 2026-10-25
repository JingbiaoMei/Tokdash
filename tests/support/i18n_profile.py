"""A stub DOM and a script builder for running dashboard code under Node.

``support.frontend`` slices functions out of ``index.html``; this module is the
other half for the tests that need those functions to touch a document: the
translation pass (``applyI18n``), the profile heatmap and its legend, the session
modal and the release-notes drawer. Each of those reads and writes elements,
attributes, ``dataset``, ``classList`` and selectors, so a test needs a DOM that
behaves like one rather than a bag of ids.

``page_script`` assembles one harness:

1. ``STUB_DOM_JS`` -- elements, a document, events, ``localStorage`` and
   ``window``, enough of each for the code under test;
2. a recorder for every function the sliced code calls but the harness does not
   define, so a call to ``renderSessionCharts`` that a test did not ask for is
   logged instead of throwing (``calls('renderSessionCharts')`` reads it back);
3. the test's own prelude (state the functions read), then the real dictionaries
   and ``t()`` when ``i18n=True``, then the real functions;
4. the test body, run inside an async function, which prints its findings with
   ``print(...)``.

A recorder is installed on ``globalThis`` only when nothing else defines the
name, and a harness file is a CommonJS module, so a function the test or the
page slice declares at top level always wins over the recorder of the same name.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from support.frontend import (
    extract_i18n_literal,
    extract_js_function,
    index_source,
    run_node,
)


STUB_DOM_JS = r"""
// ---- stub DOM ---------------------------------------------------------------
const __innerHTMLWrites = [];
const __frames = [];
const __windowListeners = {};
function __kebab(prop) { return String(prop).replace(/[A-Z]/g, (c) => '-' + c.toLowerCase()); }
function __camel(name) { return String(name).replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }

class StubStyle {
  setProperty(name, value) { this['__prop:' + name] = String(value); }
  getPropertyValue(name) { const v = this['__prop:' + name]; return v === undefined ? '' : v; }
  removeProperty(name) { const old = this.getPropertyValue(name); delete this['__prop:' + name]; return old; }
}

class StubClassList {
  constructor(node) { this.node = node; }
  __get() { return (this.node.getAttribute('class') || '').split(/\s+/).filter(Boolean); }
  __set(list) { this.node.setAttribute('class', list.join(' ')); }
  add(...names) { const list = this.__get(); names.forEach((n) => { if (!list.includes(n)) list.push(n); }); this.__set(list); }
  remove(...names) { this.__set(this.__get().filter((n) => !names.includes(n))); }
  toggle(name, force) {
    const has = this.contains(name);
    const want = force === undefined ? !has : Boolean(force);
    if (want && !has) this.add(name);
    if (!want && has) this.remove(name);
    return want;
  }
  contains(name) { return this.__get().includes(name); }
  get length() { return this.__get().length; }
  toString() { return this.__get().join(' '); }
}

function __event(type, init = {}) {
  return Object.assign({
    type, bubbles: true, defaultPrevented: false, __stopped: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this.__stopped = true; },
  }, init);
}

class StubNode {
  constructor(tag, nodeType = 1) {
    this.nodeType = nodeType;
    this.tagName = String(tag).toUpperCase();
    this.nodeName = this.tagName;
    this.childNodes = [];
    this.parentNode = null;
    this.__attrs = new Map();
    this.__listeners = {};
    this.__text = '';
    this.__html = null;
    this.style = new StubStyle();
    this.classList = new StubClassList(this);
    const self = this;
    this.dataset = new Proxy({}, {
      get(_, prop) {
        if (typeof prop !== 'string') return undefined;
        const value = self.getAttribute('data-' + __kebab(prop));
        return value === null ? undefined : value;
      },
      set(_, prop, value) { self.setAttribute('data-' + __kebab(prop), value); return true; },
      deleteProperty(_, prop) { self.removeAttribute('data-' + __kebab(prop)); return true; },
      has(_, prop) { return self.hasAttribute('data-' + __kebab(prop)); },
      ownKeys() {
        return [...self.__attrs.keys()].filter((k) => k.startsWith('data-')).map((k) => __camel(k.slice(5)));
      },
      getOwnPropertyDescriptor(_, prop) {
        const value = self.getAttribute('data-' + __kebab(prop));
        return value === null ? undefined : { value, enumerable: true, configurable: true, writable: true };
      },
    });
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.value = '';
    this.scrollLeft = 0;
    this.scrollWidth = 0;
    this.clientWidth = 0;
  }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { const kids = this.children; return kids[kids.length - 1] || null; }
  get id() { return this.getAttribute('id') || ''; }
  set id(value) { this.setAttribute('id', value); }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get title() { return this.getAttribute('title') || ''; }
  set title(value) { this.setAttribute('title', value); }
  get href() { return this.getAttribute('href') || ''; }
  set href(value) { this.setAttribute('href', value); }
  get textContent() {
    if (this.nodeType === 3) return this.__text;
    return this.__text + this.childNodes.map((n) => n.textContent).join('');
  }
  set textContent(value) {
    this.__detachAll();
    this.__html = null;
    this.__text = value === null || value === undefined ? '' : String(value);
  }
  // No parser: an innerHTML write is kept as the string it was, and logged, so a
  // test can tell markup a renderer built from text it set as text.
  get innerHTML() { return this.__html === null ? '' : this.__html; }
  set innerHTML(value) {
    __innerHTMLWrites.push(this);
    this.__detachAll();
    this.__text = '';
    this.__html = String(value);
  }
  __detachAll() { this.childNodes.forEach((n) => { n.parentNode = null; }); this.childNodes = []; }
  __adopt(node) {
    if (node === null || node === undefined) throw new TypeError('cannot append ' + node);
    if (typeof node !== 'object') {
      const text = new StubNode('#text', 3);
      text.__text = String(node);
      return [text];
    }
    if (node.nodeType === 11) {
      const kids = node.childNodes;
      node.childNodes = [];
      kids.forEach((k) => { k.parentNode = null; });
      return kids;
    }
    if (node.parentNode) node.parentNode.removeChild(node);
    return [node];
  }
  appendChild(node) {
    this.__adopt(node).forEach((k) => { k.parentNode = this; this.childNodes.push(k); });
    return node;
  }
  append(...nodes) { nodes.forEach((n) => this.appendChild(n)); }
  prepend(...nodes) {
    const adopted = nodes.flatMap((n) => this.__adopt(n));
    adopted.forEach((k) => { k.parentNode = this; });
    this.childNodes.unshift(...adopted);
  }
  insertBefore(node, ref) {
    if (!ref) return this.appendChild(node);
    const adopted = this.__adopt(node);
    const index = this.childNodes.indexOf(ref);
    adopted.forEach((k) => { k.parentNode = this; });
    this.childNodes.splice(index < 0 ? this.childNodes.length : index, 0, ...adopted);
    return node;
  }
  replaceChildren(...nodes) {
    this.__detachAll();
    this.__text = '';
    this.__html = null;
    this.append(...nodes);
  }
  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index >= 0) { this.childNodes.splice(index, 1); node.parentNode = null; }
    return node;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }
  get isConnected() { for (let n = this; n; n = n.parentNode) if (n.nodeType === 9) return true; return false; }
  setAttribute(name, value) { this.__attrs.set(String(name).toLowerCase(), String(value)); }
  getAttribute(name) { const key = String(name).toLowerCase(); return this.__attrs.has(key) ? this.__attrs.get(key) : null; }
  hasAttribute(name) { return this.__attrs.has(String(name).toLowerCase()); }
  removeAttribute(name) { this.__attrs.delete(String(name).toLowerCase()); }
  toggleAttribute(name, force) {
    const want = force === undefined ? !this.hasAttribute(name) : Boolean(force);
    if (want) this.setAttribute(name, ''); else this.removeAttribute(name);
    return want;
  }
  get attributes() { return [...this.__attrs].map(([name, value]) => ({ name, value })); }
  matches(selector) { return __matches(this, selector); }
  closest(selector) {
    for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (__matches(n, selector)) return n;
    return null;
  }
  querySelectorAll(selector) { return __descendants(this).filter((n) => __matches(n, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, listener) { (this.__listeners[type] = this.__listeners[type] || []).push(listener); }
  removeEventListener(type, listener) {
    this.__listeners[type] = (this.__listeners[type] || []).filter((fn) => fn !== listener);
  }
  dispatchEvent(event) {
    if (!event.target) event.target = this;
    for (let n = this; n; n = n.parentNode) {
      event.currentTarget = n;
      (n.__listeners[event.type] || []).slice().forEach((fn) => fn.call(n, event));
      if (event.__stopped || event.bubbles === false) break;
    }
    return !event.defaultPrevented;
  }
  click() { if (!this.disabled) this.dispatchEvent(__event('click')); }
  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) document.activeElement = document.body; }
  showModal() { this.open = true; }
  close() {
    if (!this.open) return;
    this.open = false;
    this.dispatchEvent(__event('close', { bubbles: false }));
  }
  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 }; }
  getContext() { return { canvas: this }; }
  scrollIntoView() {}
}

function __descendants(root) {
  const out = [];
  const visit = (node) => node.childNodes.forEach((child) => {
    if (child.nodeType === 1) { out.push(child); visit(child); }
  });
  visit(root);
  return out;
}

// Selectors: tag, #id, .class and [attr], [attr="v"] (=, ~=, ^=, $=, *=) in
// compounds, joined by descendant or child combinators, in comma lists.
// Anything else throws, so an unsupported selector cannot silently match nothing.
function __selectorGroups(selector) {
  const groups = [];
  let steps = [];
  let token = '';
  let depth = 0;
  let quote = null;
  let combinator = null;
  const flush = () => {
    if (!token) return;
    steps.push({ combinator: combinator || ' ', compound: __compound(token) });
    token = '';
    combinator = null;
  };
  for (const ch of String(selector)) {
    if (quote) { token += ch; if (ch === quote) quote = null; continue; }
    if (ch === '"' || ch === "'") { quote = ch; token += ch; continue; }
    if (ch === '[' || ch === '(') depth += 1;
    if (ch === ']' || ch === ')') depth -= 1;
    if (depth === 0 && ch === ',') { flush(); groups.push(steps); steps = []; combinator = null; continue; }
    if (depth === 0 && /\s/.test(ch)) { flush(); continue; }
    if (depth === 0 && ch === '>') { flush(); combinator = '>'; continue; }
    token += ch;
  }
  flush();
  if (steps.length) groups.push(steps);
  return groups;
}

function __compound(text) {
  const out = { tag: null, id: null, classes: [], attrs: [] };
  const pattern = /(\*|[a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[\s*([\w-]+)\s*(?:([~^$*]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\]\s]+))\s*)?\]/y;
  let index = 0;
  while (index < text.length) {
    pattern.lastIndex = index;
    const m = pattern.exec(text);
    if (!m || m.index !== index) throw new Error('stub DOM: unsupported selector ' + JSON.stringify(text));
    if (m[1] && index === 0) out.tag = m[1].toUpperCase();
    else if (m[1]) throw new Error('stub DOM: unsupported selector ' + JSON.stringify(text));
    if (m[2]) out.id = m[2];
    if (m[3]) out.classes.push(m[3]);
    if (m[4]) out.attrs.push({ name: m[4].toLowerCase(), op: m[5] || null, value: m[6] ?? m[7] ?? m[8] ?? null });
    index = pattern.lastIndex;
  }
  return out;
}

function __matchCompound(node, compound) {
  if (!node || node.nodeType !== 1) return false;
  if (compound.tag && compound.tag !== '*' && node.tagName !== compound.tag) return false;
  if (compound.id && node.getAttribute('id') !== compound.id) return false;
  for (const name of compound.classes) if (!node.classList.contains(name)) return false;
  for (const { name, op, value } of compound.attrs) {
    const actual = node.getAttribute(name);
    if (actual === null) return false;
    if (op === '=' && actual !== value) return false;
    if (op === '~=' && !actual.split(/\s+/).includes(value)) return false;
    if (op === '^=' && !actual.startsWith(value)) return false;
    if (op === '$=' && !actual.endsWith(value)) return false;
    if (op === '*=' && !actual.includes(value)) return false;
  }
  return true;
}

function __matchSteps(node, steps, index) {
  if (!__matchCompound(node, steps[index].compound)) return false;
  if (index === 0) return true;
  if (steps[index].combinator === '>') return __matchSteps(node.parentElement, steps, index - 1);
  for (let p = node.parentElement; p; p = p.parentElement) if (__matchSteps(p, steps, index - 1)) return true;
  return false;
}

const __parsedSelectors = new Map();
function __matches(node, selector) {
  if (!__parsedSelectors.has(selector)) __parsedSelectors.set(selector, __selectorGroups(selector));
  return __parsedSelectors.get(selector).some((steps) => __matchSteps(node, steps, steps.length - 1));
}

class StubDocument {
  constructor() {
    this.nodeType = 9;
    this.childNodes = [];
    this.__listeners = {};
    this.documentElement = new StubNode('html');
    this.documentElement.parentNode = this;
    this.head = new StubNode('head');
    this.body = new StubNode('body');
    this.documentElement.append(this.head, this.body);
    this.activeElement = this.body;
    this.readyState = 'complete';
    this.hidden = false;
  }
  getElementById(id) {
    return __descendants(this.documentElement).find((n) => n.getAttribute('id') === String(id)) || null;
  }
  querySelectorAll(selector) {
    return [this.documentElement, ...__descendants(this.documentElement)].filter((n) => __matches(n, selector));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  createElement(tag) { return new StubNode(tag); }
  createDocumentFragment() { return new StubNode('#document-fragment', 11); }
  createTextNode(text) { const node = new StubNode('#text', 3); node.__text = String(text); return node; }
  addEventListener(type, listener) { (this.__listeners[type] = this.__listeners[type] || []).push(listener); }
  removeEventListener(type, listener) {
    this.__listeners[type] = (this.__listeners[type] || []).filter((fn) => fn !== listener);
  }
}

const document = new StubDocument();

// el('button', { id: 'x', class: 'a b', 'data-i18n': 'key' }, 'text', child, ...)
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  Object.entries(attrs).forEach(([name, value]) => node.setAttribute(name, value));
  node.append(...children);
  return node;
}

// fire(node, 'keydown', { key: 'Escape' }) dispatches from `node` and bubbles.
function fire(target, type, init = {}) {
  const event = __event(type, init);
  event.target = target;
  target.dispatchEvent(event);
  return event;
}

function __storage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => { map.set(key, String(value)); },
    removeItem: (key) => { map.delete(key); },
    clear: () => map.clear(),
    dump: () => Object.fromEntries(map),
  };
}
const localStorage = __storage();
const navigator = { language: 'en-US', languages: ['en-US'] };

const window = globalThis;
window.tokdashPath = (path) => path;
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
window.addEventListener = (type, listener) => { (__windowListeners[type] = __windowListeners[type] || []).push(listener); };
window.removeEventListener = (type, listener) => {
  __windowListeners[type] = (__windowListeners[type] || []).filter((fn) => fn !== listener);
};
window.dispatchWindowEvent = (type) => (__windowListeners[type] || []).slice().forEach((fn) => fn(__event(type)));
window.requestAnimationFrame = (fn) => { __frames.push(fn); return __frames.length; };
window.cancelAnimationFrame = () => {};
window.getComputedStyle = (node) => ({
  getPropertyValue: (name) => {
    const computed = node.__computed || {};
    return name in computed ? computed[name] : node.style.getPropertyValue(name);
  },
});
window.devicePixelRatio = 1;

function flushFrames() { while (__frames.length) __frames.shift()(0); }
async function settle() { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); }
function print(value) { process.stdout.write(JSON.stringify(value)); }
function text(node) { return node ? node.textContent.replace(/\s+/g, ' ').trim() : null; }
"""


# Names a page slice may call that are syntax, not functions.
_KEYWORDS = frozenset(
    "if for while switch catch return typeof function new await async do else in of "
    "delete void yield super this with case throw".split()
)
_CALLEE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")

RECORDER_JS = r"""
// ---- recorders for what the slice calls and the harness does not define ------
const __calls = [];
function __recorder(name) {
  return function recorded(...args) { __calls.push({ name, args }); return undefined; };
}
function calls(name) { return __calls.filter((c) => c.name === name).map((c) => c.args); }
function callNames() { return __calls.map((c) => c.name); }
"""


def callees(code: str) -> list[str]:
    """Every bare name called in ``code``, in first-seen order."""
    seen: dict[str, None] = {}
    for match in _CALLEE.finditer(code):
        name = match.group(1)
        if name not in _KEYWORDS:
            seen.setdefault(name, None)
    return list(seen)


def i18n_runtime(source: str | None = None) -> str:
    """The real ``I18N`` dictionaries, ``LANG_*`` maps, ``langLocale``,
    ``detectBrowserLang`` and ``t``, with ``currentLang``/``selectedLang`` set
    to English for the test to change."""
    source = source or index_source()
    start = source.index("const LANG_LOCALES = {")
    detect = extract_js_function(source, "function detectBrowserLang() {")
    end = source.index(detect, start) + len(detect)
    return "\n".join(
        [
            extract_i18n_literal(source) + ";",
            source[start:end],
            extract_js_function(source, "function t(key, vars) {"),
            "let currentLang = 'en';",
            "let selectedLang = 'en';",
        ]
    )


def slice_statement(source: str, start_marker: str, end_marker: str) -> str:
    """Top-level code between two markers, both included (state declarations,
    listener wiring): ``extract_js_function`` only slices declarations."""
    start = source.find(start_marker)
    assert start != -1, f"{start_marker!r} not found in index.html"
    end = source.find(end_marker, start)
    assert end != -1, f"{end_marker!r} not found after {start_marker!r}"
    return source[start : end + len(end_marker)]


def slice_line(source: str, needle: str) -> str:
    """The one source line holding ``needle``."""
    index = source.find(needle)
    assert index != -1, f"{needle!r} not found in index.html"
    start = source.rfind("\n", 0, index) + 1
    end = source.find("\n", index)
    return source[start : end if end != -1 else len(source)]


def page_script(
    functions: Iterable[str] = (),
    *,
    snippets: Iterable[str] = (),
    prelude: str = "",
    body: str,
    i18n: bool = False,
    source: str | None = None,
) -> str:
    """Assemble one harness: stub DOM, recorders, prelude, i18n, code, body.

    ``functions`` are declaration signatures sliced from index.html;
    ``snippets`` are page code already sliced (state lines, listener wiring).
    Both come after the prelude, so it can seed ``localStorage`` or globals the
    sliced top-level code reads as it runs.
    """
    source = source or index_source()
    code = [extract_js_function(source, signature) for signature in functions]
    code.extend(snippets)
    stubs = ",".join(json.dumps(name) for name in callees("\n".join(code)))
    install = (
        f"for (const __name of [{stubs}]) "
        "if (!(__name in globalThis)) globalThis[__name] = __recorder(__name);\n"
    )
    parts = [STUB_DOM_JS, RECORDER_JS, install, prelude]
    if i18n:
        parts.append(i18n_runtime(source))
    parts.extend(code)
    parts.append(
        "(async () => {\n"
        + body
        + "\n})().catch((error) => { console.error(error && error.stack || error); process.exit(1); });\n"
    )
    return "\n".join(parts)


def run_page(tmp_path: Path, name: str, script: str) -> object:
    """Write ``script`` to ``tmp_path`` and return the JSON it printed."""
    harness = tmp_path / f"{name}.js"
    harness.write_text(script, encoding="utf-8")
    return json.loads(run_node(harness))


# ---- applyI18n --------------------------------------------------------------

# The translation pass and the table it reads.
APPLY_I18N_FUNCTIONS = ("const I18N_ATTR_TARGETS = {", "function applyI18n() {")

# What applyI18n reads without a typeof guard, at the values of a page with
# nothing loaded: no pricing payload, no quota rows, no refresh report.
APPLY_I18N_STATE = """
let pricingDbLastPayload = null;
let quotaSingleScopeOwner = null;
let lastQuotaServerRows = [];
let lastQuotaPayload = null;
let lastQuotaHistory = null;
let lastRefreshReport = null;
"""


# ---- release notes ----------------------------------------------------------

RELEASE_NOTES_FUNCTIONS = (
    "function getRuntimeVersion() {",
    "function renderReleaseNotes(payload) {",
    "async function loadReleaseNotes() {",
    "function loadReleaseNotesTranslation(lang = currentLang) {",
    "async function openReleaseNotes() {",
    "function closeReleaseNotes() {",
    "function initReleaseNotes() {",
    "function renderLoadingPlaceholder(element, label = t('loading')) {",
)


def release_notes_state(source: str | None = None) -> str:
    """The drawer's real module state: payload slots, the sidecar map, the
    changelog links and the two translation caches."""
    source = source or index_source()
    return slice_statement(
        source,
        "let runtimeVersionPromise = null;",
        "const releaseNotesTranslationPromises = new Map();",
    )


# The drawer as index.html marks it up, trimmed to what the code touches.
RELEASE_NOTES_DOM = """
const toggle = el('button', { id: 'releaseNotesToggle', 'aria-controls': 'releaseNotesDialog', 'aria-expanded': 'false' },
  el('span', { id: 'releaseNotesVersion' }, 'v—'));
const dialog = el('dialog', { id: 'releaseNotesDialog', class: 'release-notes-dialog' },
  el('div', { class: 'release-notes-panel' },
    el('button', { id: 'releaseNotesClose', class: 'release-notes-close' }),
    el('div', { id: 'releaseNotesList', class: 'release-notes-list' }),
    el('footer', { class: 'release-notes-footer' }, el('a', { href: '#' }, 'Full changelog'))));
document.body.append(toggle, dialog, el('span', { 'data-app-version': '' }, 'v—'));
const list = document.getElementById('releaseNotesList');
const panel = dialog.querySelector('.release-notes-panel');
const closeButton = document.getElementById('releaseNotesClose');
const changelogLink = dialog.querySelector('.release-notes-footer a');
function entries() {
  return list.querySelectorAll('.release-notes-entry').map((entry) => ({
    version: text(entry.querySelector('.release-notes-number')),
    sections: entry.querySelectorAll('.release-notes-section').map((section) => ({
      label: text(section.querySelector('.release-notes-section-label')),
      items: section.querySelectorAll('li').map((item) => ({ text: item.textContent, children: item.children.length })),
    })),
  }));
}
"""
