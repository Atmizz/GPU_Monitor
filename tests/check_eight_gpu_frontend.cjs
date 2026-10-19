// Run after simulate_eight_gpus.py. No browser, HTTP requests or remote hosts.
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
        this.classList = { toggle() {}, add() {}, remove() {} };
    }
    set innerHTML(value) { this.html = value; this.children = []; }
    get innerHTML() {
        return this.html ?? this.textContent.replaceAll('&', '&amp;')
            .replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
    }
    appendChild(child) { this.children.push(child); }
    addEventListener() {}
    querySelector() { return null; }
}

const fixturePath = process.argv[2] || path.join(__dirname,
    '../reports/eight_gpu_simulation/frontend_fixture.json');
const node = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const elements = new Map();
const dot = new Element();
const context = vm.createContext({
    window: { addEventListener() {} },
    document: {
        addEventListener() {},
        getElementById: id => elements.get(id),
        createElement: () => new Element(),
        querySelectorAll: () => [{ dataset: { hostname: node.hostname }, querySelector: () => dot }]
    },
    localStorage: { getItem: () => null },
    requestAnimationFrame() {},
    console
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/main.js'), 'utf8'), context);

assert.equal(node.gpus.length, 8);
const failed = node.gpus.filter(gpu => gpu.error);
assert.equal(failed.length, 1);
assert.equal(failed[0]['pci.bus_id'], '0000:1b:00.0');
assert.equal(context.gpuOccupancyLabel(failed[0]), 'Unavailable');
assert.equal(context.isGpuFree(failed[0]), false);
assert.equal(context.getGpuColor(node, failed[0]), '#6c757d');
assert.equal(node.gpus.filter(context.isGpuFree).length, 4);
assert.equal(context.filterGpuDataByFreeGpu([node])[0].gpus.length, 4);

const card = context.createGpuCard(node, failed[0], `${node.hostname}::7`);
assert.match(card.innerHTML, /Unavailable/);
assert.match(card.innerHTML, /Unknown Error/);
assert.match(card.innerHTML, /-\/- GB/);
assert.doesNotMatch(card.innerHTML, /gpu-occupants-free/);

elements.set('free-model-shortcuts-list', new Element());
elements.set('mobile-free-model-shortcuts-list', new Element());
context.renderFreeModelShortcuts([node]);
assert.match(elements.get('free-model-shortcuts-list').children[0].innerHTML, /4 \/ 8/);
context.updateServerShortcutStatus([node]);
assert.equal(dot.className, 'server-status-dot degraded');

vm.runInContext(`dataTable = {
    clear() {}, rows: { add(rows) { globalThis.tableRows = rows; } }, draw() {}
}`, context);
context.updateTable([node]);
assert.equal(context.tableRows.length, 8);
const failedRow = context.tableRows.find(row => row[6].includes('Unknown Error'));
assert.ok(failedRow);
assert.equal(failedRow[2], '-');
assert.equal(failedRow[3], '-');
console.log('PASS eight-GPU frontend: 8 rows, 7 readable GPUs, 4 free, 1 unavailable, degraded node, original error retained.');
