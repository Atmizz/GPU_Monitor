// Run with: node tests/test_frontend.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
    constructor() {
        this.children = [];
        this.dataset = {};
        this.style = {};
        this.textContent = '';
        this.classes = new Set();
        this.classList = {
            toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
            add: name => this.classes.add(name),
            remove: name => this.classes.delete(name)
        };
    }
    set innerHTML(value) { this.html = value; this.children = []; }
    get innerHTML() {
        return this.html ?? this.textContent.replaceAll('&', '&amp;').replaceAll('<', '&lt;')
            .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
    }
    appendChild(child) { this.children.push(child); }
    addEventListener() {}
    querySelector() { return null; }
}

const elements = new Map();
const buttons = [];
const context = vm.createContext({
    window: { addEventListener() {} },
    document: {
        addEventListener() {},
        getElementById: id => elements.get(id),
        createElement: () => new Element(),
        querySelectorAll: () => buttons
    },
    localStorage: { getItem: () => null },
    requestAnimationFrame() {},
    console
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/main.js'), 'utf8'), context);

const healthy = {
    index: '0', name: 'NVIDIA RTX 4090', users: 0, user_processes: '',
    'utilization.gpu': 0, 'temperature.gpu': 40, 'power.draw': 0,
    'memory.used': 0, 'memory.total': 24576, memory: 0
};
const failed = {
    ...healthy, index: '1', users: null, error: 'GPU handle unavailable',
    'utilization.gpu': null, 'memory.used': null, 'memory.total': null, memory: null
};
const unknown = { ...healthy, index: '2', users: null, process_error: 'Query timed out' };
const busy = { ...healthy, index: '3', users: 1, user_processes: 'alice(python,1024M)' };
const node = { hostname: 'test-host', gpus: [healthy, failed, unknown, busy] };

assert.equal(context.isGpuFree(healthy), true);
assert.equal(context.isGpuFree(failed), false);
assert.equal(context.isGpuFree(unknown), false);
assert.equal(context.isGpuFree(busy), false);
assert.equal(context.isGpuFree({ users: null }), false);
assert.equal(context.gpuOccupancyLabel(failed), 'Unavailable');
assert.equal(context.gpuOccupancyLabel(unknown), 'Unknown');
assert.equal(context.gpuOccupancyLabel(busy), 'alice');
assert.equal(context.getGpuColor(node, failed), '#6c757d');
assert.equal(context.getGpuColor(node, unknown), '#6c757d');
assert.equal(context.getGpuColor(node, { users: null }), '#6c757d');
assert.equal(context.filterGpuDataByFreeGpu([node])[0].gpus.length, 1);
assert.equal(context.filterGpuDataByFreeGpu([{ ...node, error: 'SSH failed' }]).length, 0);

context.fixture = [node];
vm.runInContext("selectedFreeModel = 'RTX 4090'", context);
assert.equal(context.applyGpuFilters([node])[0].gpus.length, 1);
vm.runInContext('selectedFreeModel = null', context);

elements.set('free-model-shortcuts-list', new Element());
elements.set('mobile-free-model-shortcuts-list', new Element());
context.renderFreeModelShortcuts([node]);
const freeButton = elements.get('free-model-shortcuts-list').children[0];
assert.match(freeButton.innerHTML, /1 \/ 4/);

const badCard = context.createGpuCard(node, failed, 'test-host::1');
assert.match(badCard.innerHTML, /Unavailable/);
assert.doesNotMatch(badCard.innerHTML, /gpu-occupants-free/);
assert.match(badCard.innerHTML, /-\/- GB/);
const unknownCard = context.createGpuCard(node, unknown, 'test-host::2');
assert.match(unknownCard.innerHTML, /Unknown/);
assert.doesNotMatch(unknownCard.innerHTML, /gpu-occupants-free/);
const goodCard = context.createGpuCard(node, healthy, 'test-host::0');
assert.match(goodCard.innerHTML, /gpu-util fw-bold ms-1">0<\/span>/);

// Reuse the same DOM card across failure and recovery; stale free styling must
// disappear immediately and return only after a successful process query.
const cardParts = new Map([
    '.card', '.gpu-name', '.gpu-occupants', '.gpu-index', '.gpu-temp',
    '.gpu-power', '.gpu-util', '.gpu-users', '.gpu-mem-text', '.gpu-memory-progress'
].map(selector => [selector, new Element()]));
const wrapper = { querySelector: selector => cardParts.get(selector) };
context.updateGpuCard(wrapper, node, healthy);
assert.equal(cardParts.get('.gpu-occupants').classes.has('gpu-occupants-free'), true);
context.updateGpuCard(wrapper, node, failed);
assert.equal(cardParts.get('.gpu-occupants').textContent, 'Unavailable');
assert.equal(cardParts.get('.gpu-occupants').classes.has('gpu-occupants-free'), false);
assert.equal(cardParts.get('.gpu-util').textContent, '-');
assert.equal(cardParts.get('.card').style.backgroundColor, '#6c757d');
context.updateGpuCard(wrapper, node, healthy);
assert.equal(cardParts.get('.gpu-occupants').textContent, 'Free');
assert.equal(cardParts.get('.gpu-occupants').classes.has('gpu-occupants-free'), true);
assert.equal(cardParts.get('.gpu-util').textContent, '0');
assert.equal(cardParts.get('.card').style.backgroundColor, '#198754');

vm.runInContext(`dataTable = {
    clear() {}, rows: { add(rows) { globalThis.tableRows = rows; } }, draw() {}
}`, context);
context.updateTable([node]);
assert.equal(context.tableRows.length, 4);
assert.match(context.tableRows[0][3], />0%<\/span>/);
assert.equal(context.tableRows[1][2], '-');
assert.match(context.tableRows[1][6], /GPU handle unavailable/);
assert.match(context.tableRows[2][6], /Unknown/);

const dot = new Element();
buttons.push({ dataset: { hostname: 'test-host' }, querySelector: () => dot });
context.updateServerShortcutStatus([node]);
assert.equal(dot.className, 'server-status-dot degraded');
context.updateServerShortcutStatus([{ hostname: 'test-host', gpus: [healthy] }]);
assert.equal(dot.className, 'server-status-dot online');
console.log('Frontend regression checks passed: isolation, free counts, unknown occupancy, failure/recovery, table and server status.');
