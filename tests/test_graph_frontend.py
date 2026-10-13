"""Execute the actual graph UI functions without a browser or external services."""

import shutil
import subprocess
from pathlib import Path

import pytest


ASSET = Path(__file__).parents[1] / "researchwiki/web/static/app.js"

HARNESS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('const graphColors =');
const end = source.indexOf('async function searchOriginalSource(', start);
assert(start >= 0 && end > start, 'Graph function block must exist');
const elements = new Map();
function element(key) {
  if (!elements.has(key)) {
    const classes = new Set(key === '#graph-g6' ? ['hidden'] : []);
    const el = {
      innerHTML: '', textContent: '', value: '', clientWidth: 920, style: {},
      attributes: {}, listeners: {}, disabled: false,
      classList: {
        add: (...values) => values.forEach(value => classes.add(value)),
        remove: (...values) => values.forEach(value => classes.delete(value)),
        contains: value => classes.has(value),
        toggle(value, force) {
          const enabled = force === undefined ? !classes.has(value) : force;
          enabled ? classes.add(value) : classes.delete(value);
          return enabled;
        },
      },
      setAttribute: (name, value) => { el.attributes[name] = String(value); },
      addEventListener: (name, callback) => { el.listeners[name] = callback; },
      closest: () => element('#stage-body'),
    };
    elements.set(key, el);
  }
  return elements.get(key);
}
const nodes = [
  {id: 'research:paper', name: 'A真实论文', type: 'PAPER'},
  {id: 'research:method', name: '证据约束方法', type: 'METHOD'},
  {id: 'research:dataset', name: '真实数据集', type: 'DATASET'},
];
const edges = [
  {id: 'edge:method', source: nodes[0].id, target: nodes[1].id,
   semantic: 'REFERENCES_METHOD', label: '关联方法'},
  {id: 'edge:dataset', source: nodes[0].id, target: nodes[2].id,
   semantic: 'MENTIONS_DATASET', label: '提及数据集'},
];
const payload = {
  nodes, edges, center: nodes[0].id,
  stats: {nodes: nodes.length, edges: edges.length},
  node_types: {PAPER: 1, METHOD: 1, DATASET: 1},
};
const overview = {documents: 5, page_types: {paper: 4}, statuses: {INDEXED: 4, FAILED: 1}};
const requests = [], toasts = [];
const state = {graph: {
  nodes: [], edges: [], visibleTypes: new Set(), scale: 1, panX: 0, panY: 0,
  center: '', renderer: 'svg', g6: null,
}};
let response = payload;
const sandbox = {
  state, window: {setTimeout, clearTimeout},
  $: element, $$: (selector, root) => {
    if (selector !== '[data-graph-paper]' || !root) return [];
    return [...root.innerHTML.matchAll(/data-graph-paper="([^"]+)"/g)].map(match => {
      const button = element('paper-button:' + match[1]);
      button.dataset = {graphPaper: match[1]};
      return button;
    });
  },
  escapeHTML: value => String(value ?? '').replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#039;'),
  renderMetric: (icon, value, label) => `<span>${icon}:${value}:${label}</span>`,
  toast: message => toasts.push(message),
  evidenceMarkup: () => '', goToWikiPage: () => {},
  api: async path => {
    requests.push(path);
    if (path === '/api/overview') return overview;
    if (path.startsWith('/api/wiki/pages/')) return {sources: []};
    if (response instanceof Error) throw response;
    return response;
  },
};
vm.createContext(sandbox);
vm.runInContext(source.slice(start, end), sandbox);
function assertSvgObjects(expectedNodes, expectedEdges) {
  const markup = element('#graph-canvas').innerHTML;
  assert.strictEqual((markup.match(/data-graph-node=/g) || []).length, expectedNodes.length);
  assert.strictEqual((markup.match(/<path class="graph-edge /g) || []).length, expectedEdges.length);
  for (const node of expectedNodes) assert(markup.includes(`data-graph-node="${node.id}"`));
  assert(!/NaN|Infinity|undefined/.test(markup));
  assert(!element('#graph-canvas').classList.contains('hidden'));
  assert(element('#graph-g6').classList.contains('hidden'));
}
"""


@pytest.fixture
def run_graph_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable for graph frontend runtime regressions")

    def run(scenario):
        script = HARNESS + "\n(async () => {\n" + scenario + "\n})().catch(error => { console.error(error); process.exitCode = 1; });"
        completed = subprocess.run(
            [node, "-e", script, str(ASSET)], capture_output=True, text=True, timeout=15
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    return run


def test_local_load_renders_without_an_undefined_graph_global(run_graph_js):
    run_graph_js(r"""
assert(!Object.hasOwn(sandbox, 'graph'));
await sandbox.loadGraph('');
const graphRequests = requests.filter(path => path.startsWith('/api/graph/research?'));
assert.strictEqual(graphRequests.length, 1);
assert(graphRequests[0].startsWith('/api/graph/research?center=&'));
assert.strictEqual(state.graph.center, nodes[0].id);
assert.strictEqual(state.graph.selectedId, nodes[0].id);
assertSvgObjects(nodes, edges);
assert(element('#graph-view-badge').textContent.includes('局部'));
assert(element('#graph-detail').innerHTML.includes('A真实论文'));
assert(element('#graph-empty-status').classList.contains('hidden'));
""")


def test_default_load_shows_all_papers_in_the_global_view(run_graph_js):
    run_graph_js(r"""
const otherPaper = {id: 'research:other-paper', name: '第二篇基础导入论文', type: 'PAPER'};
overview.page_types.paper = 2;
response = {
  ...payload, nodes: [...nodes, otherPaper],
  stats: {nodes: 4, edges: 2}, node_types: {PAPER: 2, METHOD: 1, DATASET: 1},
};
await sandbox.loadGraph();
const graphRequest = requests.find(path => path.startsWith('/api/graph/research?'));
assert(graphRequest.startsWith('/api/graph/research?center=__all__&'));
assert.strictEqual(state.graph.center, '__all__');
assert.strictEqual(state.graph.nodes.filter(node => node.type === 'PAPER').length, 2);
assertSvgObjects(response.nodes, edges);
assert(element('#graph-global').classList.contains('active'));
assert(element('#graph-view-badge').textContent.includes('全局'));
assert(!element('#graph-view-badge').textContent.includes('一跳'));
assert(element('#graph-canvas').innerHTML.includes(otherPaper.id));
assert(element('#graph-type-filters').innerHTML.includes('PAPER'));
assert(element('#graph-type-filters').innerHTML.includes('2'));
const paperList = element('#graph-paper-list');
assert(!paperList.classList.contains('hidden'));
assert.strictEqual((paperList.innerHTML.match(/data-graph-paper=/g) || []).length, 2);
assert(paperList.innerHTML.includes(`data-graph-paper="${otherPaper.id}"`));
assert(paperList.innerHTML.includes(otherPaper.name));
""")


def test_global_paper_navigation_opens_the_chosen_paper_locally(run_graph_js):
    run_graph_js(r"""
const otherPaper = {id: 'research:other-paper', name: '第二篇基础导入论文', type: 'PAPER'};
response = {...payload, nodes: [...nodes, otherPaper]};
await sandbox.loadGraph();
const button = sandbox.$$('[data-graph-paper]', element('#graph-paper-list'))
  .find(item => item.dataset.graphPaper === otherPaper.id);
assert(button && button.listeners.click);
response = {nodes: [otherPaper], edges: [], center: otherPaper.id,
            stats: {nodes: 4, edges: 2}, node_types: {PAPER: 1}};
await button.listeners.click();
assert(requests.some(path => path.startsWith('/api/graph/research?center=' + encodeURIComponent(otherPaper.id))));
assert.strictEqual(state.graph.center, otherPaper.id);
assert.strictEqual(state.graph.selectedId, otherPaper.id);
assert(element('#graph-view-badge').textContent.includes('局部'));
assert(element('#graph-paper-list').classList.contains('hidden'));
assertSvgObjects([otherPaper], []);
""")


def test_overview_failure_does_not_hide_the_graph(run_graph_js):
    run_graph_js(r"""
sandbox.api = async path => {
  requests.push(path);
  if (path === '/api/overview') throw new Error('overview is unavailable');
  return payload;
};
await sandbox.loadGraph();
assert.strictEqual(state.graph.center, '__all__');
assertSvgObjects(nodes, edges);
assert(element('#graph-empty-status').classList.contains('hidden'));
assert(!element('#graph-status').textContent.includes('加载失败'));
""")


def test_local_metrics_distinguish_uploaded_global_and_visible_papers(run_graph_js):
    run_graph_js(r"""
await sandbox.loadGraph('');
const metrics = element('#graph-stats').innerHTML;
assert(metrics.includes(':5:已上传资料'));
assert(metrics.includes(':4:全库论文节点'));
assert(metrics.includes(':1:当前视图论文'));
assert(metrics.includes(':2:当前视图关系'));
assert(element('#graph-library-note').textContent.includes('局部') || element('#graph-library-note').innerHTML.includes('局部'));
""")


def test_global_view_includes_unconnected_objects_without_fabricated_edges(run_graph_js):
    run_graph_js(r"""
const isolated = {id: 'research:isolated', name: '未连线主题', type: 'TOPIC'};
response = {...payload, nodes: [...nodes, isolated], node_types: {...payload.node_types, TOPIC: 1}};
await sandbox.loadGraph('__all__');
assert.strictEqual(state.graph.center, '__all__');
assert.strictEqual(state.graph.nodes.length, 4);
assert.strictEqual(state.graph.edges.length, 2);
assertSvgObjects(response.nodes, edges);
assert(element('#graph-global').classList.contains('active'));
assert(element('#graph-global').textContent.includes('切换局部'));
assert(element('#graph-canvas').innerHTML.includes('暂无可展示关系的对象'));
assert(element('#graph-stage-subtitle').textContent.includes('关系不补造'));
""")


def test_api_failure_displays_an_escaped_error_instead_of_a_blank_canvas(run_graph_js):
    run_graph_js(r"""
element('#graph-canvas').innerHTML = 'old graph';
response = new Error('网络失效 <script>bad()</script>');
await sandbox.loadGraph();
assert.strictEqual(element('#graph-canvas').innerHTML, '');
const message = element('#graph-empty-status').innerHTML;
assert(message.includes('暂时无法读取图谱'));
assert(message.includes('&lt;script&gt;'));
assert(!message.includes('<script>'));
assert(!element('#graph-empty-status').classList.contains('hidden'));
assert.strictEqual(element('#graph-status').textContent, '加载失败');
assert.strictEqual(state.graph.nodes.length, 0);
assert.strictEqual(state.graph.edges.length, 0);
""")


def test_chinese_labels_preserve_short_english_terms_and_escape_titles(run_graph_js):
    run_graph_js(r"""
const lines = sandbox.graphLabelLines('BERT提示学习（BERT-Prompt）', 174);
assert.strictEqual(Array.from(lines).join(''), 'BERT提示学习（BERT-Prompt）');
assert(lines.some(line => line.includes('BERT-Prompt')));
const longLines = sandbox.graphLabelLines('一篇很长很长很长很长很长很长很长的论文标题', 150);
assert.strictEqual(longLines.length, 2);
assert(longLines[1].endsWith('…'));
response = {...payload, nodes: nodes.map(node => ({...node, name: '<script>bad()</script>'}))};
await sandbox.loadGraph();
const markup = element('#graph-canvas').innerHTML;
assert(markup.includes('&lt;script&gt;'));
assert(!markup.includes('<script>'));
""")


def test_empty_graph_and_empty_filters_have_distinct_messages(run_graph_js):
    run_graph_js(r"""
response = {nodes: [], edges: [], stats: {nodes: 0, edges: 0}, node_types: {}};
await sandbox.loadGraph();
assertSvgObjects([], []);
assert(element('#graph-empty-status').innerHTML.includes('还没有知识对象'));
assert(!element('#graph-empty-status').classList.contains('hidden'));
response = payload;
await sandbox.loadGraph();
state.graph.visibleTypes.clear();
sandbox.renderGraph();
assertSvgObjects([], []);
assert(element('#graph-empty-status').innerHTML.includes('当前筛选没有对象'));
assert.strictEqual(state.graph.nodes.length, nodes.length);
""")


def test_g6_load_renders_the_same_real_nodes_and_edges(run_graph_js):
    run_graph_js(r"""
let captured, rendered = 0, selected;
class FakeGraph {
  constructor(options) { captured = options; }
  on() {}
  render() { rendered += 1; return Promise.resolve(); }
  setElementState(value) { selected = value; return Promise.resolve(); }
  destroy() {}
}
sandbox.window.G6 = {Graph: FakeGraph};
state.graph.renderer = 'g6';
await sandbox.loadGraph('');
await Promise.resolve();
assert.strictEqual(rendered, 1, 'Selecting the initial node must not recreate the renderer');
assert.deepStrictEqual(Array.from(captured.data.nodes, value => value.id), nodes.map(value => value.id));
assert.deepStrictEqual(Array.from(captured.data.edges, value => value.id), edges.map(value => value.id));
assert(element('#graph-canvas').classList.contains('hidden'));
assert(!element('#graph-g6').classList.contains('hidden'));
assert.strictEqual(state.graph.selectedId, nodes[0].id);
assert(selected);
""")


@pytest.mark.parametrize("failure", ["constructor", "render"])
def test_g6_failures_restore_the_local_renderer(run_graph_js, failure):
    run_graph_js("const failure = " + repr(failure) + ";\n" + r"""
let destroyed = 0;
class FailingGraph {
  constructor() { if (failure === 'constructor') throw new Error('G6 unavailable'); }
  on() {}
  render() { return Promise.reject(new Error('G6 layout unavailable')); }
  destroy() { destroyed += 1; }
}
sandbox.window.G6 = {Graph: FailingGraph};
state.graph.renderer = 'g6';
await sandbox.loadGraph();
await new Promise(resolve => setImmediate(resolve));
assert.strictEqual(state.graph.renderer, 'svg');
assert.strictEqual(state.graph.g6, null);
assertSvgObjects(nodes, edges);
assert(toasts.some(message => message.includes('本地布局')));
if (failure === 'render') assert.strictEqual(destroyed, 1);
""")


@pytest.mark.parametrize("width", [360, 920])
def test_large_global_and_mobile_layouts_have_finite_coordinates(run_graph_js, width):
    run_graph_js("element('#graph-canvas').clientWidth = " + str(width) + ";\n" + r"""
const many = Array.from({length: 45}, (_, index) => ({
  id: 'research:extra' + index, name: '一个很长的科研对象标题_' + index,
  type: ['METHOD', 'DATASET', 'TOPIC'][index % 3],
}));
response = {...payload, nodes: [...nodes, ...many]};
await sandbox.loadGraph('__all__');
const before = JSON.stringify({nodes: state.graph.nodes, edges: state.graph.edges});
const positions = sandbox.graphPositions(state.graph.nodes, state.graph.edges);
assert.strictEqual(positions.size, response.nodes.length);
for (const [id, position] of positions) {
  assert(response.nodes.some(node => node.id === id));
  for (const field of ['x', 'y', 'w', 'h']) assert(Number.isFinite(position[field]), id + ':' + field);
}
assert(Number.isFinite(state.graph.dimensions.width));
assert(Number.isFinite(state.graph.dimensions.height));
assert.strictEqual(state.graph.dimensions.mobile, element('#graph-canvas').clientWidth < 680);
assert.strictEqual(before, JSON.stringify({nodes: state.graph.nodes, edges: state.graph.edges}));
assertSvgObjects(response.nodes, edges);
""")


def test_a_late_request_does_not_overwrite_a_newer_global_graph(run_graph_js):
    run_graph_js(r"""
let resolveLocal;
sandbox.api = path => {
  requests.push(path);
  if (path === '/api/overview') return Promise.resolve(overview);
  if (path.includes('center=__all__')) return Promise.resolve({...payload, nodes: [...nodes, {id: 'research:new', name: '新主题', type: 'TOPIC'}]});
  return new Promise(resolve => { resolveLocal = resolve; });
};
const local = sandbox.loadGraph('');
await sandbox.loadGraph('__all__');
resolveLocal(payload);
await local;
assert.strictEqual(state.graph.center, '__all__');
assert.strictEqual(state.graph.nodes.length, 4);
assert(element('#graph-canvas').innerHTML.includes('research:new'));
""")
