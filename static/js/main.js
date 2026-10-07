// ===== 全局变量 =====
let dataTable = null;
let isRefreshEnabled = true;
let refreshTimeout = null;
const REFRESH_RATE = 2000;

let sortableInstance = null; 
let sidebarSortableInstance = null; 

let selectedServer = null;
let selectedUser = null;
let selectedFreeModel = null; 

let lastServerStatusData = null;
let lastGpuData = null;     // 最近一轮完整数据,表格从折叠展开时用它补刷,不必等下一个轮询
let tableCollapsed = true;  // All Nodes and GPUs 表格折叠状态:折叠期间 fetchData 跳过表格重绘
const gpuCardMap = new Map();

// ===== 服务器分组区块状态 =====
const serverSectionMap = new Map(); // hostname -> section 元素
let collapsedServers = loadCollapsedServers();

function loadCollapsedServers() {
    try {
        const raw = JSON.parse(localStorage.getItem('gpumonitor.collapsedServers') || '[]');
        return new Set(Array.isArray(raw) ? raw.map(String) : []);
    } catch (e) {
        return new Set();
    }
}

function saveCollapsedServers() {
    try {
        localStorage.setItem('gpumonitor.collapsedServers', JSON.stringify(Array.from(collapsedServers)));
    } catch (e) { /* localStorage 不可用时仅本次会话内生效 */ }
}

function getOrCreateServerSection(hostname, alias) {
    let section = serverSectionMap.get(hostname);
    if (section) return section;

    section = document.createElement('div');
    section.className = 'server-section' + (collapsedServers.has(hostname) ? ' collapsed' : '');
    section.dataset.hostname = hostname;

    const header = document.createElement('div');
    header.className = 'server-section-header';
    header.title = `Click to expand / collapse ${hostname}`;
    header.innerHTML = `
        <i class="fas fa-chevron-down section-chevron"></i>
        <i class="fas fa-server section-server-icon"></i>
        <span class="section-title text-truncate" title="${escapeHtml(hostname)}">${escapeHtml(alias || hostname)}</span>
        <span class="badge bg-secondary section-gpu-count ms-auto"></span>
    `;
    header.addEventListener('click', function () {
        const isCollapsed = section.classList.toggle('collapsed');
        if (isCollapsed) collapsedServers.add(hostname);
        else collapsedServers.delete(hostname);
        saveCollapsedServers();
    });
    section.appendChild(header);

    const body = document.createElement('div');
    body.className = 'row g-2 g-sm-3 server-section-body';
    section.appendChild(body);

    serverSectionMap.set(hostname, section);
    return section;
}

const SHORTCUT_WIDTH = 180;
const SHORTCUT_GAP = 14;
const SHORTCUT_EDGE = 12;
const SHORTCUT_MIN_SCREEN = 900;

// ===== 颜色管理 =====
// 色板固定 20 色：
//   - 索引 0：空闲专属绿色（独占，不分配给任何用户）
//   - 索引 1~19：用户颜色池，共 19 个
// 用户颜色直接按用户名分配：当前所有活跃用户名排序后，按序号依次循环
// 取色。同一用户在所有服务器、所有显卡上颜色一致；活跃用户超过 19 个时
// 不同用户可能重色；用户集变化时部分用户的颜色会随之顺移。
// 19 个用户色在 OKLab 感知空间按「两两最小距离最大化」搜索得出（同时
// 满足：白字对比度>=3:1、避开绿色色相带、远离空闲绿/离线灰/Free胶囊青），
// 色号顺序经过重排使相邻色号（即按用户名排序相邻的用户）差异尽量大。
// 离线/错误卡片使用灰色状态色（OFFLINE_COLOR），不属于用户色板。
const GPU_COLOR_PALETTE = [
    '#198754', // 0: 空闲专属绿
    '#f66370', // 1: 珊瑚红
    '#1d54c1', // 2: 蓝
    '#e16c10', // 3: 橙
    '#0f92f7', // 4: 天蓝
    '#ac1b18', // 5: 深红
    '#06a2ae', // 6: 青
    '#8a2d98', // 7: 紫
    '#b98e1b', // 8: 金黄
    '#b476ef', // 9: 淡紫
    '#7c5500', // 10: 深棕
    '#878fd2', // 11: 蓝灰
    '#877819', // 12: 橄榄黄
    '#7864c8', // 13: 紫罗兰
    '#b56350', // 14: 陶红
    '#086990', // 15: 深青
    '#c64a9a', // 16: 玫红
    '#65508e', // 17: 暗紫
    '#c37faa', // 18: 粉
    '#8f4965'  // 19: 梅紫
];
const IDLE_COLOR_INDEX = 0;
const OFFLINE_COLOR = '#6c757d';

// 当前活跃用户名（升序），每次拉取数据后重建，是用户取色的唯一依据
let activeUsernames = [];

function refreshActiveUsernames(data) {
    const names = new Set();
    (data || []).forEach(function (node) {
        const gpus = Array.isArray(node.gpus) ? node.gpus : [];
        gpus.forEach(function (gpu) {
            extractGpuUsers(gpu).forEach(function (username) { names.add(username); });
        });
    });
    activeUsernames = Array.from(names).sort();
}

function getUserColor(username) {
    const userColorCount = GPU_COLOR_PALETTE.length - 1; // 19 个用户色
    let index = activeUsernames.indexOf(String(username));
    if (index === -1) index = activeUsernames.length; // 兜底：视作排在当前用户之后
    return GPU_COLOR_PALETTE[1 + (index % userColorCount)];
}

function getGpuColor(node, gpu) {
    if (node?.error || gpu?.index === 'Err') return OFFLINE_COLOR;
    const users = extractGpuUsers(gpu);
    // 绿色独占：无占用进程的空闲显卡
    if (users.length === 0) return GPU_COLOR_PALETTE[IDLE_COLOR_INDEX];
    // 多人共用时取列表首位用户的颜色，完整占用信息见悬浮提示
    return getUserColor(users[0]);
}

// 图例：空闲/离线徽章 + 用户色胶囊（颜色 -> 用户名，随活跃用户实时更新）
let legendUserSignature = null;

function renderUserColorLegend() {
    const container = document.getElementById('gpu-color-legend');
    if (!container) return;

    // 用户集未变化时跳过重建，避免每 2 秒一次的无谓 DOM 更新
    const signature = activeUsernames.join('\n');
    if (signature === legendUserSignature) return;
    legendUserSignature = signature;

    container.innerHTML = '';

    const idleBadge = document.createElement('span');
    idleBadge.className = 'badge rounded-pill';
    idleBadge.style.backgroundColor = GPU_COLOR_PALETTE[IDLE_COLOR_INDEX];
    idleBadge.textContent = 'Free (Available)';
    container.appendChild(idleBadge);

    const offlineBadge = document.createElement('span');
    offlineBadge.className = 'badge rounded-pill';
    offlineBadge.style.backgroundColor = OFFLINE_COLOR;
    offlineBadge.textContent = 'Offline / Error';
    container.appendChild(offlineBadge);

    const note = document.createElement('span');
    note.className = 'text-muted small';
    note.textContent = 'User colors:';
    container.appendChild(note);

    if (activeUsernames.length === 0) {
        const empty = document.createElement('span');
        empty.className = 'text-muted small';
        empty.textContent = 'No active users';
        container.appendChild(empty);
        return;
    }

    // 每位活跃用户一枚胶囊，底色即其在显卡卡片上的颜色；
    // 超过 19 人循环复用色板时，同色用户会出现同色胶囊
    activeUsernames.forEach(function (username) {
        const pill = document.createElement('span');
        pill.className = 'badge rounded-pill legend-user-pill';
        pill.style.backgroundColor = getUserColor(username);
        pill.textContent = username;
        pill.title = username;
        container.appendChild(pill);
    });
}

// ===== 显卡档次/算力权重映射表（分值越高越靠前） =====
const GPU_MODEL_TIERS = {
    'H200': 1200, 'H100': 1100, 'H800': 1050, 'A100': 1000,
    'A800': 950, 'L40S': 860, 'L40': 850, 'RTX 6000 Ada': 840,
    'RTX A6000': 820, 'RTX A5000': 780, 'L4': 750, 'A40': 740,
    'A30': 720, 'V100': 700, 'A10': 680, 'T4': 600,
    'RTX 5090': 590, 'RTX 4090': 580, 'RTX 4080': 550,
    'RTX 3090 Ti': 520, 'RTX 3090': 500, 'RTX 3080': 450, 'RTX 2080 Ti': 400
};

function getGpuModelWeight(modelName) {
    const cleanName = modelName.trim();
    if (GPU_MODEL_TIERS[cleanName] !== undefined) {
        return GPU_MODEL_TIERS[cleanName];
    }
    for (const [key, weight] of Object.entries(GPU_MODEL_TIERS)) {
        if (cleanName.includes(key)) return weight;
    }
    return 0; 
}

function closeMobileOffcanvas() {
    const offcanvasEl = document.getElementById('mobileFilters');
    if (offcanvasEl) {
        const instance = bootstrap.Offcanvas.getInstance(offcanvasEl);
        if (instance) instance.hide();
    }
}

// ===== 卡片高亮联动函数 =====
function highlightCards(keys) {
    clearHighlights();
    keys.forEach(function(key) {
        const cardWrapper = gpuCardMap.get(key);
        if (cardWrapper) cardWrapper.classList.add('gpu-card-highlight');
    });
}

function clearHighlights() {
    document.querySelectorAll('.gpu-card-highlight').forEach(el => el.classList.remove('gpu-card-highlight'));
}

// ===== 服务器区块实时排序核心逻辑 (拖拽时触发) =====
function reorderGpuCardsByHostnames(hostnames) {
    const container = document.getElementById('gpu-cards-container');
    if (!container) return;

    const sections = Array.from(container.querySelectorAll(':scope > .server-section'));
    const sectionMap = new Map(sections.map(sec => [sec.dataset.hostname, sec]));

    let idx = 0;
    const placed = new Set();
    hostnames.forEach(function (hostname) {
        const section = sectionMap.get(String(hostname));
        if (!section || placed.has(section)) return;
        placed.add(section);
        if (container.children[idx] !== section) {
            container.insertBefore(section, container.children[idx] || null);
        }
        idx++;
    });

    // 未出现在目标顺序中的区块保持原有相对顺序，排在最后
    sections.forEach(function (section) {
        if (placed.has(section)) return;
        if (container.children[idx] !== section) {
            container.insertBefore(section, container.children[idx] || null);
        }
        idx++;
    });
}

function syncGpuCardsOrderToSidebar() {
    const hostnames = Array.from(document.querySelectorAll('#server-shortcuts-list .server-shortcut-btn[data-hostname]'))
        .map(btn => btn.dataset.hostname);
    reorderGpuCardsByHostnames(hostnames);
}

function sortDataBySidebar(data) {
    const uiOrder = Array.from(document.querySelectorAll('#server-shortcuts-list .server-shortcut-btn[data-hostname]'))
        .map(btn => btn.dataset.hostname);
    
    if (uiOrder.length > 0) {
        data.sort((a, b) => {
            let indexA = uiOrder.indexOf(a.hostname);
            let indexB = uiOrder.indexOf(b.hostname);
            if (indexA === -1) indexA = 999;
            if (indexB === -1) indexB = 999;
            return indexA - indexB;
        });
    }
    return data;
}

// ===== 快捷栏位置自适应 =====
function updateShortcutPositions() {
    const userSidebar = document.getElementById('user-gpu-shortcuts');
    const serverSidebar = document.getElementById('server-shortcuts');
    const main = document.getElementById('main-content');
    const menuBtn = document.getElementById('mobile-menu-btn'); 

    if (!userSidebar || !serverSidebar || !main) return;

    const vw = window.innerWidth;
    let needMobileMenu = false;

    if (vw < SHORTCUT_MIN_SCREEN) {
        userSidebar.classList.add('overlap-hidden');
        serverSidebar.classList.add('overlap-hidden');
        needMobileMenu = true; 
    } else {
        const mainRect = main.getBoundingClientRect();
        const uW = userSidebar.offsetWidth || SHORTCUT_WIDTH;
        const sW = serverSidebar.offsetWidth || SHORTCUT_WIDTH;

        if (mainRect.left >= uW + SHORTCUT_GAP + SHORTCUT_EDGE) {
            userSidebar.style.left = Math.max(SHORTCUT_EDGE, mainRect.left - uW - SHORTCUT_GAP) + 'px';
            userSidebar.style.right = '';
            userSidebar.classList.remove('overlap-hidden');
        } else {
            userSidebar.classList.add('overlap-hidden');
            userSidebar.style.left = '';
            needMobileMenu = true;
        }

        if (vw - mainRect.right >= sW + SHORTCUT_GAP + SHORTCUT_EDGE) {
            serverSidebar.style.right = Math.max(SHORTCUT_EDGE, vw - mainRect.right - sW - SHORTCUT_GAP) + 'px';
            serverSidebar.style.left = '';
            serverSidebar.classList.remove('overlap-hidden');
        } else {
            serverSidebar.classList.add('overlap-hidden');
            serverSidebar.style.right = '';
            needMobileMenu = true;
        }
    }

    if (menuBtn) {
        if (needMobileMenu) {
            menuBtn.classList.remove('d-none');
            menuBtn.classList.add('d-flex'); 
        } else {
            menuBtn.classList.add('d-none');
            menuBtn.classList.remove('d-flex');
        }
    }
}
window.addEventListener('resize', updateShortcutPositions);

// 后台标签页的定时器会被浏览器节流(后端会随之进入空闲降频),
// 回到标签页时立即拉一帧补上滞后的数据
document.addEventListener('visibilitychange', function () {
    if (!document.hidden && isRefreshEnabled) fetchData();
});

// ===== DOM Ready =====
document.addEventListener('DOMContentLoaded', function () {
    updateShortcutPositions();
    renderUserColorLegend();

    dataTable = new DataTable('#dataTable', {
        paging: true,
        pageLength: 25,
        lengthChange: true,
        searching: true,
        info: true,
        order: [[0, 'asc'], [1, 'asc']],
        autoWidth: false,
        columnDefs: [
            { targets: 0, width: '12%' }, { targets: 1, width: '25%' },
            { targets: 2, width: '8%' }, { targets: 3, width: '10%' },
            { targets: 4, width: '15%' }, { targets: 5, width: '10%' },
            { targets: 6, width: '21%' }, { targets: '_all', className: 'small align-middle' }
        ]
    });

    // ===== All Nodes and GPUs 表格默认折叠 =====
    // 每次页面加载都默认收起；会话内切换仅保存在内存中（折叠状态 tableCollapsed 为全局变量,
    // fetchData 据此跳过不可见表格的重绘）
    const tableCard = document.getElementById('nodes-table-card');
    const tableHeader = document.getElementById('nodes-table-header');

    const applyTableCollapsed = function () {
        tableCard.classList.toggle('collapsed', tableCollapsed);
    };
    applyTableCollapsed();

    tableHeader.addEventListener('click', function () {
        tableCollapsed = !tableCollapsed;
        applyTableCollapsed();
        if (!tableCollapsed && dataTable) {
            // 折叠期间表格未随轮询刷新,展开时先用最近一轮数据补刷;
            // 容器 display:none 期间 DataTables 列宽测量不准,随后重新校准
            if (lastGpuData) {
                try { updateTable(applyGpuFilters(lastGpuData)); } catch (e) { console.error('Table update failed:', e); }
            }
            try { dataTable.columns.adjust(); } catch (e) { }
        }
    });

    document.getElementById('toggle-refresh').addEventListener('click', function (e) {
        e.stopPropagation(); // Pause 按钮在标题栏内，避免触发折叠切换
        isRefreshEnabled = !isRefreshEnabled;
        if (isRefreshEnabled) {
            this.innerHTML = '<i class="fas fa-pause me-1"></i> Pause';
            this.classList.replace('btn-outline-danger', 'btn-outline-primary');
            fetchData();
        } else {
            this.innerHTML = '<i class="fas fa-play me-1"></i> Resume';
            this.classList.replace('btn-outline-primary', 'btn-outline-danger');
            clearTimeout(refreshTimeout);
        }
    });

    document.getElementById('settingsModal').addEventListener('show.bs.modal', loadSettings);
    
    document.getElementById('add-server-form').addEventListener('submit', async function (e) {
        e.preventDefault();
        await addServer();
    });

    document.getElementById('bulk-add-btn').addEventListener('click', async function () {
        await addServersBulk();
    });

    loadServerShortcuts();
    fetchData();
});

// ===== 数据标准化 =====
function normalizeGpuData(raw) {
    if (Array.isArray(raw)) return raw;
    if (raw && Array.isArray(raw.data)) return raw.data;
    if (raw && Array.isArray(raw.nodes)) return raw.nodes;
    if (raw && Array.isArray(raw.result)) return raw.result;
    return [];
}

function normalizeNode(node) {
    if (!node || typeof node !== 'object') return null;
    let gpus = node.gpus;
    if (!Array.isArray(gpus)) {
        if (gpus && typeof gpus === 'object') gpus = Object.values(gpus);
        else gpus = [];
    }
    return { ...node, hostname: String(node.hostname || node.host || node.node || 'Unknown'), gpus };
}

function normalizeNodes(raw) {
    return normalizeGpuData(raw).map(normalizeNode).filter(Boolean);
}

function simplifyModelName(name) {
    if (!name) return 'Unknown';
    return name.replace(/NVIDIA\s+/i, '').replace(/GeForce\s+/i, '').trim();
}

// 服务器显示名：有别名用别名，否则回退 hostname
function serverDisplayName(node) {
    if (!node || typeof node !== 'object') return 'Unknown';
    return String(node.alias || node.hostname || 'Unknown');
}

function escapeAttr(value) {
    return escapeHtml(value).replace(/"/g, '&quot;');
}

// ===== 服务器快捷栏 =====
async function loadServerShortcuts() {
    try {
        const res = await fetch('/api/admin/servers', { cache: 'no-store' });
        if (!res.ok) throw new Error('Failed to load servers');
        const servers = await res.json();
        renderServerShortcuts(Array.isArray(servers) ? servers : []);
    } catch (e) { 
        console.error('Load server shortcuts failed:', e); 
    }
}

function renderServerShortcuts(servers) {
    const renderTarget = (containerId, isMobile) => {
        const container = document.getElementById(containerId);
        if(!container) return;
        container.innerHTML = '';

        const allBtn = document.createElement('button');
        allBtn.type = 'button';
        allBtn.className = 'server-shortcut-btn' + (selectedServer === null ? ' active' : '');
        allBtn.innerHTML = `<i class="fas fa-layer-group"></i><span class="server-shortcut-name">All Servers</span><span class="server-status-dot all-status"></span>`;
        allBtn.title = 'Show all servers';

        if(!isMobile) {
            allBtn.addEventListener('mouseenter', function() {
                const allKeys = Array.from(gpuCardMap.keys());
                highlightCards(allKeys);
            });
            allBtn.addEventListener('mouseleave', clearHighlights);
        }

        allBtn.addEventListener('click', function () {
            const changed = selectedServer !== null || selectedUser !== null || selectedFreeModel !== null;
            selectedServer = null;
            selectedUser = null;
            selectedFreeModel = null;
            updateAllActiveStates();
            if (changed) fetchData();
            if (isMobile) closeMobileOffcanvas();
        });
        container.appendChild(allBtn);

        if (!servers || servers.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'server-shortcuts-empty';
            empty.textContent = 'No servers';
            container.appendChild(empty);
            return;
        }

        servers.forEach(function (server) {
            const hostname = String(server.hostname || '');
            const displayName = String(server.alias || hostname);
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'server-shortcut-btn' + (selectedServer === hostname ? ' active' : '');
            btn.dataset.hostname = hostname;
            btn.title = `Show ${displayName} (${hostname})`;
            btn.innerHTML = `<i class="fas fa-server"></i><span class="server-shortcut-name">${escapeHtml(displayName)}</span><span class="server-status-dot unknown" data-status-host="${escapeHtml(hostname)}"></span>`;

            if(!isMobile) {
                btn.addEventListener('mouseenter', function() {
                    const serverKeys = [];
                    for (const key of gpuCardMap.keys()) {
                        if (key.startsWith(`${hostname}::`)) serverKeys.push(key);
                    }
                    highlightCards(serverKeys);
                });
                btn.addEventListener('mouseleave', clearHighlights);
            }

            btn.addEventListener('click', function () {
                if (selectedServer === hostname && selectedUser === null && selectedFreeModel === null) {
                    selectedServer = null;
                } else {
                    selectedServer = hostname;
                    selectedUser = null;
                    selectedFreeModel = null;
                }
                updateAllActiveStates();
                fetchData();
                if (isMobile) closeMobileOffcanvas();
            });
            container.appendChild(btn);
        });

        // 右侧服务器快捷栏可直接拖拽排序
        if (!isMobile && containerId === 'server-shortcuts-list' && servers.length > 0) {
            if (sidebarSortableInstance) sidebarSortableInstance.destroy();
            
            sidebarSortableInstance = new Sortable(container, {
                draggable: '.server-shortcut-btn[data-hostname]', 
                animation: 150,
                ghostClass: 'sortable-ghost',
                onChange: function () {
                    syncGpuCardsOrderToSidebar();
                },
                onEnd: async function () {
                    syncGpuCardsOrderToSidebar(); 
                    const buttons = container.querySelectorAll('.server-shortcut-btn[data-hostname]');
                    const newOrder = Array.from(buttons).map(btn => btn.dataset.hostname).filter(Boolean);
                    try {
                        await fetch('/api/admin/servers/reorder', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(newOrder)
                        });
                        if (document.getElementById('settingsModal').classList.contains('show')) {
                            loadSettings();
                        }
                        fetchData();
                    } catch (e) {
                        console.error('Save sidebar order failed', e);
                        loadServerShortcuts(); 
                    }
                }
            });
        }
    };

    renderTarget('server-shortcuts-list', false);
    renderTarget('mobile-server-shortcuts-list', true);
    
    if (lastServerStatusData) updateServerShortcutStatus(lastServerStatusData);
    requestAnimationFrame(updateShortcutPositions);
}

function updateServerShortcutStatus(data) {
    if (!Array.isArray(data)) return;
    lastServerStatusData = data;
    document.querySelectorAll('.server-shortcut-btn[data-hostname]').forEach(function (btn) {
        const hostname = btn.dataset.hostname;
        const dot = btn.querySelector('.server-status-dot');
        if (!dot) return;
        const node = data.find(item => String(item.hostname) === hostname);
        if (!node) {
            dot.className = 'server-status-dot offline';
            dot.title = 'Offline / No response';
            return;
        }
        if (node.error) {
            dot.className = 'server-status-dot offline';
            dot.title = `Offline: ${node.error}`;
            return;
        }
        dot.className = 'server-status-dot online';
        dot.title = 'Online';
    });
}

function markAllServerShortcutsOffline() {
    document.querySelectorAll('.server-status-dot[data-status-host]').forEach(function (dot) {
        dot.className = 'server-status-dot offline';
        dot.title = 'Offline / Monitor connection failed';
    });
}

// ===== 激活状态同步 =====
function updateAllActiveStates() {
    updateShortcutActiveState();
    updateUserShortcutActiveState();
    updateFreeGpuShortcutActiveState();
    updateFreeModelActiveState();
}

function updateShortcutActiveState() {
    document.querySelectorAll('.server-shortcut-btn').forEach(function (btn) {
        btn.classList.remove('active');
    });
    if (selectedServer === null) {
        document.querySelectorAll('#server-shortcuts-list .server-shortcut-btn:first-child, #mobile-server-shortcuts-list .server-shortcut-btn:first-child').forEach(first => {
            if (first) first.classList.add('active');
        });
        return;
    }
    document.querySelectorAll('.server-shortcut-btn[data-hostname]').forEach(function (btn) {
        if (btn.dataset.hostname === selectedServer) btn.classList.add('active');
    });
}

function updateUserShortcutActiveState() {
    document.querySelectorAll('.user-shortcut-btn[data-username]').forEach(function (btn) {
        btn.classList.toggle('active', btn.dataset.username === selectedUser);
    });
}

function updateFreeGpuShortcutActiveState() {
    document.querySelectorAll('.user-free-gpu-card').forEach(card => {
        card.classList.toggle('active', selectedUser === '__FREE_GPUS__');
    });
}

function updateFreeModelActiveState() {
    document.querySelectorAll('.user-shortcut-btn[data-freemodel]').forEach(function (btn) {
        btn.classList.toggle('active', btn.dataset.freemodel === selectedFreeModel);
    });
}

// ===== 用户解析 =====
function extractGpuUsers(gpu) {
    if (!gpu) return [];
    const raw = String(gpu.user_processes || gpu.users || '').trim();
    if (!raw || raw === 'None' || raw === '-') return [];
    const users = [];

    const matches = raw.matchAll(/([A-Za-z_][A-Za-z0-9_.-]*)\s*\(/g);
    for (const match of matches) {
        if (!users.includes(match[1])) users.push(match[1]);
    }

    if (users.length === 0) {
        const parts = raw.split(/[\n;, \t]+/);
        for (const part of parts) {
            if (part && /^[A-Za-z_][A-Za-z0-9_.-]*$/.test(part)) {
                if (!users.includes(part)) users.push(part);
            }
        }
    }
    return users;
}

function buildUserGpuMap(data) {
    const userMap = new Map();
    (data || []).forEach(function (node) {
        const gpus = Array.isArray(node.gpus) ? node.gpus : [];
        gpus.forEach(function (gpu) {
            extractGpuUsers(gpu).forEach(function (username) {
                if (!userMap.has(username)) userMap.set(username, []);
                userMap.get(username).push({
                    hostname: node.hostname, index: gpu.index, name: gpu.name
                });
            });
        });
    });
    return userMap;
}

// ===== 过滤 =====
function filterGpuDataByFreeGpu(data) {
    return (data || []).map(function (node) {
        const gpus = Array.isArray(node.gpus) ? node.gpus.filter(function (gpu) {
            return extractGpuUsers(gpu).length === 0;
        }) : [];
        return gpus.length === 0 ? null : { ...node, gpus };
    }).filter(Boolean);
}

function filterGpuDataByUser(data, username) {
    return (data || []).map(function (node) {
        const gpus = Array.isArray(node.gpus) ? node.gpus.filter(function (gpu) {
            return extractGpuUsers(gpu).includes(username);
        }) : [];
        return gpus.length === 0 ? null : { ...node, gpus };
    }).filter(Boolean);
}

function applyGpuFilters(data) {
    let displayData = Array.isArray(data) ? data : [];
    if (selectedServer !== null) {
        displayData = displayData.filter(function (node) { return node.hostname === selectedServer; });
    }
    
    if (selectedUser === '__FREE_GPUS__') {
        displayData = filterGpuDataByFreeGpu(displayData);
    } else if (selectedUser !== null) {
        displayData = filterGpuDataByUser(displayData, selectedUser);
    } else if (selectedFreeModel !== null) {
        displayData = displayData.map(node => {
            const gpus = Array.isArray(node.gpus) ? node.gpus.filter(gpu => {
                const shortName = simplifyModelName(gpu.name);
                return extractGpuUsers(gpu).length === 0 && shortName === selectedFreeModel;
            }) : [];
            return gpus.length === 0 ? null : { ...node, gpus };
        }).filter(Boolean);
    }
    return displayData;
}

// ===== 渲染 Free Models =====
let freeModelShortcutsSignature = null;  // 上次渲染的签名,内容不变时跳过 DOM 重建

function renderFreeModelShortcuts(data) {
    const modelStats = {};
    let globalTotal = 0, globalFree = 0;
    const globalFreeKeys = [];

    (data || []).forEach(node => {
        const gpus = Array.isArray(node.gpus) ? node.gpus : [];
        gpus.forEach(gpu => {
            globalTotal++;
            const isFree = extractGpuUsers(gpu).length === 0;
            if (isFree) {
                globalFree++;
                globalFreeKeys.push(`${node.hostname}::${gpu.index}`);
            }
            const shortName = simplifyModelName(gpu.name);
            if (!modelStats[shortName]) {
                modelStats[shortName] = { total: 0, free: 0, freeKeys: [] };
            }
            modelStats[shortName].total++;
            if (isFree) {
                modelStats[shortName].free++;
                modelStats[shortName].freeKeys.push(`${node.hostname}::${gpu.index}`);
            }
        });
    });

    const freePercent = globalTotal > 0 ? Math.round((globalFree / globalTotal) * 100) : 0;
    const models = Object.keys(modelStats).sort((a, b) => {
        const weightA = getGpuModelWeight(a), weightB = getGpuModelWeight(b);
        if (weightA !== weightB) return weightB - weightA;
        return a.localeCompare(b);
    });

    // 签名除 Free/总数外还包含具体空闲卡号:数量不变但空闲卡换代(一退一占)时,
    // 悬停高亮的目标卡也会变,必须重建。选中态由 updateAllActiveStates 单独维护,跳过不受影响
    const signature = models
        .map(m => `${m}:${modelStats[m].free}/${modelStats[m].total}:${modelStats[m].freeKeys.join('+')}`)
        .join('|') + `#${globalFree}/${globalTotal}:${globalFreeKeys.join('+')}`;
    if (signature === freeModelShortcutsSignature) return;
    freeModelShortcutsSignature = signature;

    const renderTarget = (containerId, isMobile) => {
        const container = document.getElementById(containerId);
        if (!container) return;
        container.innerHTML = '';

        const freeCard = document.createElement('button');
        freeCard.type = 'button';
        freeCard.className = 'user-free-gpu-card' + (selectedUser === '__FREE_GPUS__' ? ' active' : '');
        freeCard.innerHTML = `
            <div class="d-flex align-items-center justify-content-between gap-2">
                <span class="user-free-gpu-left"><i class="fas fa-circle-check"></i><span class="user-free-gpu-label">Free GPUs (All)</span></span>
                <span class="user-free-gpu-percent">${freePercent}%</span>
            </div>
            <div class="d-flex align-items-baseline justify-content-between mt-1">
                <span class="user-free-gpu-count">${globalFree} / ${globalTotal}</span>
                <span class="small opacity-75">available</span>
            </div>
            <div class="user-free-gpu-progress"><div class="user-free-gpu-progress-bar" style="width:${freePercent}%"></div></div>`;
        
        if (globalFree === 0) {
            freeCard.classList.add('empty-btn');
            freeCard.title = `No GPUs available`;
        } else {
            freeCard.title = `Show all ${globalFree} available GPUs`;
            if(!isMobile) {
                freeCard.addEventListener('mouseenter', () => highlightCards(globalFreeKeys));
                freeCard.addEventListener('mouseleave', clearHighlights);
            }
            freeCard.addEventListener('click', () => {
                if (selectedUser === '__FREE_GPUS__') selectedUser = null;
                else { selectedUser = '__FREE_GPUS__'; selectedServer = null; selectedFreeModel = null; }
                updateAllActiveStates();
                fetchData();
                if(isMobile) closeMobileOffcanvas();
            });
        }
        container.appendChild(freeCard);

        if (models.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'user-gpu-shortcuts-empty';
            empty.textContent = 'No models detected';
            container.appendChild(empty);
            return;
        }

        models.forEach(model => {
            const stat = modelStats[model];
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'user-shortcut-btn' + (selectedFreeModel === model ? ' active' : '');
            btn.dataset.freemodel = model;
            
            const freeColorStyle = stat.free > 0 ? 'color: #20c997; font-weight: 600;' : 'color: #6c757d; font-weight: 600;';

            btn.innerHTML = `
                <span class="user-shortcut-left">
                    <i class="fas fa-microchip" style="color: #adb5bd;"></i>
                    <span class="user-shortcut-name" title="${escapeHtml(model)}">${escapeHtml(model)}</span>
                </span>
                <span class="user-gpu-count">
                    <span style="${freeColorStyle}">${stat.free}</span> <span class="opacity-50">/ ${stat.total}</span>
                </span>
            `;

            if (stat.free === 0) {
                btn.classList.add('empty-btn');
                btn.title = `No free ${model} GPUs available`;
            } else {
                btn.title = `Show free ${model} GPUs`;
                if (!isMobile) {
                    btn.addEventListener('mouseenter', () => highlightCards(stat.freeKeys));
                    btn.addEventListener('mouseleave', clearHighlights);
                }
                btn.addEventListener('click', () => {
                    if (selectedFreeModel === model) selectedFreeModel = null;
                    else { selectedFreeModel = model; selectedUser = null; selectedServer = null; }
                    updateAllActiveStates();
                    fetchData();
                    if (isMobile) closeMobileOffcanvas();
                });
            }
            container.appendChild(btn);
        });
    };

    renderTarget('free-model-shortcuts-list', false);
    renderTarget('mobile-free-model-shortcuts-list', true);
}

// ===== 用户快捷栏 =====
let userShortcutsSignature = null;  // 上次渲染的签名,内容不变时跳过 DOM 重建

function renderUserGpuShortcuts(data) {
    const userMap = buildUserGpuMap(data);
    
    const userGpus = Array.from(userMap.keys()).map(username => {
        const gpuList = userMap.get(username) || [];
        const uniqueGpus = [], seen = new Set();
        gpuList.forEach(function (gpu) {
            const key = `${gpu.hostname}::${gpu.index}`;
            if (!seen.has(key)) {
                seen.add(key);
                uniqueGpus.push(gpu);
            }
        });
        return { username, uniqueGpus };
    });

    userGpus.sort((a, b) => {
        if (b.uniqueGpus.length !== a.uniqueGpus.length) {
            return b.uniqueGpus.length - a.uniqueGpus.length;
        }
        return a.username.localeCompare(b.username);
    });

    // 签名包含每位用户占用的具体卡号:只比数量不够,「一退一占、数量不变」时
    // 悬停高亮的目标卡也会变,必须重建。选中态由 updateAllActiveStates 单独维护,
    // 跳过重建不影响点击交互
    const signature = userGpus
        .map(u => `${u.username}:${u.uniqueGpus.map(g => `${g.hostname}::${g.index}`).join('+')}`)
        .join('|');
    if (signature === userShortcutsSignature) return;
    userShortcutsSignature = signature;

    const renderTarget = (containerId, isMobile) => {
        const container = document.getElementById(containerId);
        if (!container) return;
        container.innerHTML = '';

        if (userGpus.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'user-gpu-shortcuts-empty';
            empty.textContent = 'No active users';
            container.appendChild(empty);
            return;
        }

        userGpus.forEach(({ username, uniqueGpus }) => {
            const userBtn = document.createElement('button');
            userBtn.type = 'button';
            userBtn.className = 'user-shortcut-btn' + (selectedUser === username ? ' active' : '');
            userBtn.dataset.username = username;
            userBtn.innerHTML = `
                <span class="user-shortcut-left">
                    <span class="user-color-dot" style="background-color:${getUserColor(username)}" title="User color"></span>
                    <span class="user-shortcut-name" title="${escapeHtml(username)}">${escapeHtml(username)}</span>
                </span>
                <span class="user-gpu-count">${uniqueGpus.length} GPU${uniqueGpus.length > 1 ? 's' : ''}</span>
            `;
            userBtn.title = `Show ${username} GPUs`;

            if(!isMobile) {
                const userKeys = uniqueGpus.map(gpu => `${gpu.hostname}::${gpu.index}`);
                userBtn.addEventListener('mouseenter', () => highlightCards(userKeys));
                userBtn.addEventListener('mouseleave', clearHighlights);
            }

            userBtn.addEventListener('click', () => {
                if (selectedUser === username) selectedUser = null;
                else { selectedUser = username; selectedServer = null; selectedFreeModel = null; }
                updateAllActiveStates();
                fetchData();
                if(isMobile) closeMobileOffcanvas();
            });
            container.appendChild(userBtn);
        });
    }

    renderTarget('user-gpu-shortcuts-list', false);
    renderTarget('mobile-user-shortcuts-list', true);
    requestAnimationFrame(updateShortcutPositions);
}

// ===== 获取数据 =====
async function fetchData() {
    const statusBadge = document.getElementById('connection-status');
    try {
        const response = await fetch('/api/gpustat/all', { cache: 'no-store' });
        if (!response.ok) throw new Error(`Server Error: ${response.status}`);
        const raw = await response.json();

        let data = normalizeNodes(raw);
        
        // 核心：让数据在渲染前强制遵循侧边栏UI上的顺序
        data = sortDataBySidebar(data);
        lastGpuData = data;

        // 先按本帧数据重建活跃用户表，卡片与快捷栏的用户取色都以此为依据
        refreshActiveUsernames(data);

        try {
            const displayData = applyGpuFilters(data);
            updateCards(data, displayData);
        } catch (e) { console.error('GPU Card update failed:', e); }

        try {
            renderUserGpuShortcuts(data);
            renderFreeModelShortcuts(data);
        } catch (e) { console.error('Sidebar UI update failed:', e); }

        try { renderUserColorLegend(); } catch (e) { console.error('Legend update failed:', e); }

        try { updateServerShortcutStatus(data); } catch (e) { console.error('Server shortcut update failed:', e); }

        if (!tableCollapsed) {
            // 表格折叠时跳过重绘:隐藏状态下 draw() 纯属白算,展开时会用 lastGpuData 补刷
            try {
                const displayData = applyGpuFilters(data);
                updateTable(displayData);
            } catch (e) { console.error('Table update failed:', e); }
        }

        document.getElementById('update-timestamp').textContent = new Date().toLocaleTimeString();
        statusBadge.className = 'badge bg-success';
        statusBadge.textContent = 'Connected';

    } catch (error) {
        console.error('Fetch GPU data failed:', error);
        statusBadge.className = 'badge bg-danger';
        statusBadge.textContent = 'Disconnected';
        markAllServerShortcutsOffline();
    } finally {
        if (isRefreshEnabled) {
            clearTimeout(refreshTimeout);
            refreshTimeout = setTimeout(fetchData, REFRESH_RATE);
        }
    }
}

// ===== GPU Cards 逻辑 (按服务器分组) =====
function updateCards(allData, displayData) {
    const container = document.getElementById('gpu-cards-container');
    if (!container || !Array.isArray(allData)) return;

    const visibleKeys = new Set();
    (displayData || []).forEach(function (node) {
        const hostname = String(node.hostname || 'Unknown');
        let gpus = Array.isArray(node.gpus) ? node.gpus : [];
        if (gpus.length === 0 && node.error) visibleKeys.add(`${hostname}::Err`);
        gpus.forEach(gpu => visibleKeys.add(`${hostname}::${String(gpu.index || '')}`));
    });

    // 显式选中某台服务器时自动展开，避免被折叠状态挡住
    if (selectedServer !== null && collapsedServers.has(selectedServer)) {
        collapsedServers.delete(selectedServer);
        saveCollapsedServers();
        const selectedSection = serverSectionMap.get(selectedServer);
        if (selectedSection) selectedSection.classList.remove('collapsed');
    }

    const currentKeys = new Set();
    const currentHosts = new Set();
    let sectionIndex = 0;

    allData.forEach(function (node) {
        if (!node) return;
        const hostname = String(node.hostname || 'Unknown');
        currentHosts.add(hostname);

        const section = getOrCreateServerSection(hostname, node.alias);
        // 别名可能被修改，保持区块标题与最新数据同步
        const titleEl = section.querySelector('.section-title');
        if (titleEl) {
            const displayName = node.alias || hostname;
            if (titleEl.textContent !== displayName) titleEl.textContent = displayName;
            if (titleEl.title !== hostname) titleEl.title = hostname;
        }
        if (container.children[sectionIndex] !== section) {
            container.insertBefore(section, container.children[sectionIndex] || null);
        }
        sectionIndex++;

        let gpus = Array.isArray(node.gpus) ? node.gpus : [];
        const isOfflineNode = gpus.length === 0 && node.error;
        if (isOfflineNode) {
            gpus = [{
                index: 'Err', name: node.error, 'temperature.gpu': '-', 'utilization.gpu': '-',
                'power.draw': '-', 'memory.used': 0, 'memory.total': 0, memory: 0, users: 'Connection Error',
                user_processes: 'Connection Error'
            }];
        }

        // 区块头徽章：GPU 数量 + 空闲数 / 离线状态
        const countBadge = section.querySelector('.section-gpu-count');
        if (countBadge) {
            if (isOfflineNode) {
                countBadge.className = 'badge bg-danger section-gpu-count ms-auto';
                countBadge.textContent = 'Offline';
            } else {
                const freeCount = gpus.filter(gpu => extractGpuUsers(gpu).length === 0).length;
                const freeHtml = freeCount > 0
                    ? `<span class="section-free-pill">${freeCount} free</span>`
                    : `<span class="section-free-zero">${freeCount} free</span>`;
                countBadge.className = 'badge bg-secondary section-gpu-count ms-auto';
                countBadge.innerHTML = `${gpus.length} GPU${gpus.length > 1 ? 's' : ''} · ${freeHtml}`;
            }
        }

        const body = section.querySelector('.server-section-body');
        let domIndex = 0;
        let sectionHasVisibleCard = false;

        gpus.forEach(function (gpu) {
            if (!gpu) return;
            const index = String(gpu.index || '');
            const key = `${hostname}::${index}`;
            currentKeys.add(key);

            let card = gpuCardMap.get(key);
            if (!card) {
                card = createGpuCard(node, gpu, key);
                gpuCardMap.set(key, card);
            }
            updateGpuCard(card, node, gpu);

            if (body.children[domIndex] !== card) {
                body.insertBefore(card, body.children[domIndex] || null);
            }
            domIndex++;

            if (visibleKeys.has(key)) {
                card.classList.remove('d-none');
                sectionHasVisibleCard = true;
            } else {
                card.classList.add('d-none');
            }
        });

        // 过滤后整台服务器无可见卡片时，隐藏整个区块
        section.classList.toggle('d-none', !sectionHasVisibleCard);
    });

    // 清理已下线服务器的卡片与区块
    for (const [key, card] of gpuCardMap.entries()) {
        if (!currentKeys.has(key)) {
            disposeGpuCard(card);
            gpuCardMap.delete(key);
        }
    }
    for (const [hostname, section] of serverSectionMap.entries()) {
        if (!currentHosts.has(hostname)) {
            section.remove();
            serverSectionMap.delete(hostname);
        }
    }
}

function createGpuCard(node, gpu, key) {
    const cardColor = getGpuColor(node, gpu);
    const memUsedGB = getMemoryUsedGB(gpu);
    const memTotalGB = getMemoryTotalGB(gpu);
    const memPercent = Number(gpu.memory) || 0;
    const isErrCard = gpu.index === 'Err';
    const occupants = isErrCard ? [] : extractGpuUsers(gpu);
    const occupantsText = occupants.length > 0 ? escapeHtml(occupants.join(', ')) : (isErrCard ? '—' : 'Free');
    const occupantsTitle = occupants.length > 0 ? occupants.join(', ') : (isErrCard ? 'Unknown (offline)' : 'No active user');

    const wrapper = document.createElement('div');
    wrapper.className = 'col-12 col-xs-6 col-sm-6 col-md-4 col-lg-3';
    wrapper.dataset.gpuKey = key;
    wrapper.dataset.hostname = node.hostname || '';
    wrapper.dataset.gpuIndex = gpu.index !== undefined ? String(gpu.index) : 'Err';
    wrapper.id = 'gpu-card-' + safeId(key);

    wrapper.innerHTML = `
        <div class="card text-white h-100 shadow-sm" style="background-color:${cardColor};" data-card-color="${cardColor}">
            <div class="card-body pb-2">
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <div class="fw-bold text-truncate gpu-name" title="${escapeHtml(gpu.name || '')}">${escapeHtml(gpu.name || '')}</div>
                    <span class="badge bg-dark bg-opacity-25 gpu-index ms-1 flex-shrink-0">${escapeHtml(String(gpu.index || ''))}</span>
                </div>
                <div class="d-flex align-items-center small mb-2">
                    <i class="fas fa-user me-1 opacity-50 flex-shrink-0"></i>
                    <span class="gpu-occupants text-truncate${occupants.length === 0 && !isErrCard ? ' gpu-occupants-free' : ''}" title="${escapeHtml(occupantsTitle)}">${occupantsText}</span>
                </div>
                <div class="d-flex justify-content-between small"><span>Memory</span><span class="gpu-mem-text opacity-75">${memUsedGB}/${memTotalGB} GB</span></div>
                <div class="progress progress-subtle"><div class="progress-bar bg-light gpu-memory-progress" role="progressbar" style="width:${memPercent}%"></div></div>
            </div>
            <div class="card-footer text-white small bg-black bg-opacity-10 border-0">
                <div class="row text-center g-1">
                    <div class="col-3" title="Temp"><i class="fas fa-thermometer-half"></i><span class="gpu-temp fw-bold ms-1">${escapeHtml(String(gpu['temperature.gpu'] || '-'))}</span>°</div>
                    <div class="col-3" title="Power"><i class="fas fa-bolt"></i><span class="gpu-power fw-bold ms-1">${escapeHtml(String(gpu['power.draw'] || '-'))}</span>W</div>
                    <div class="col-3" title="Util"><i class="fas fa-tachometer-alt"></i><span class="gpu-util fw-bold ms-1">${escapeHtml(String(gpu['utilization.gpu'] || '-'))}</span>%</div>
                    <div class="col-3" title="Users"><i class="fas fa-users"></i><span class="gpu-users fw-bold ms-1">${escapeHtml(String(gpu.users || gpu.user_processes || ''))}</span></div>
                </div>
            </div>
        </div>
    `;

    const cardInner = wrapper.querySelector('.card');
    if (cardInner && typeof bootstrap !== 'undefined') {
        wrapper._gpuTooltip = new bootstrap.Tooltip(cardInner, { html: true, title: buildTooltipContent(node, gpu) });
    }
    return wrapper;
}

function updateGpuCard(wrapper, node, gpu) {
    if (!wrapper) return;
    const cardInner = wrapper.querySelector('.card');
    if (!cardInner) return;

    const cardColor = getGpuColor(node, gpu);
    if (cardInner.dataset.cardColor !== cardColor) {
        cardInner.style.backgroundColor = cardColor;
        cardInner.dataset.cardColor = cardColor;
    }

    const setText = function (selector, value) {
        const el = wrapper.querySelector(selector);
        if (el && el.textContent.trim() !== String(value)) el.textContent = String(value);
    };

    const nameEl = wrapper.querySelector('.gpu-name');
    if (nameEl) {
        const name = String(gpu.name || '');
        if (nameEl.textContent.trim() !== name) {
            nameEl.textContent = name;
            nameEl.title = name;
        }
    }
    const occupantsEl = wrapper.querySelector('.gpu-occupants');
    if (occupantsEl) {
        const isErrCard = gpu.index === 'Err';
        const occupants = isErrCard ? [] : extractGpuUsers(gpu);
        const text = occupants.length > 0 ? occupants.join(', ') : (isErrCard ? '—' : 'Free');
        if (occupantsEl.textContent.trim() !== text) occupantsEl.textContent = text;
        occupantsEl.classList.toggle('gpu-occupants-free', occupants.length === 0 && !isErrCard);
        occupantsEl.title = occupants.length > 0 ? text : (isErrCard ? 'Unknown (offline)' : 'No active user');
    }
    setText('.gpu-index', String(gpu.index || ''));
    setText('.gpu-temp', String(gpu['temperature.gpu'] || '-'));
    setText('.gpu-power', String(gpu['power.draw'] || '-'));
    setText('.gpu-util', String(gpu['utilization.gpu'] || '-'));
    setText('.gpu-users', String(gpu.users || gpu.user_processes || ''));

    const memUsedGB = getMemoryUsedGB(gpu);
    const memTotalGB = getMemoryTotalGB(gpu);
    const memText = `${memUsedGB}/${memTotalGB} GB`;
    const memTextEl = wrapper.querySelector('.gpu-mem-text');
    if (memTextEl && memTextEl.textContent.trim() !== memText) memTextEl.textContent = memText;

    const memPercent = Number(gpu.memory) || 0;
    const progress = wrapper.querySelector('.gpu-memory-progress');
    if (progress) {
        const width = Math.max(0, Math.min(100, memPercent)) + '%';
        if (progress.style.width !== width) progress.style.width = width;
    }

    if (wrapper._gpuTooltip) {
        try { wrapper._gpuTooltip.setContent({ '.tooltip-inner': buildTooltipContent(node, gpu) }); } 
        catch (e) { console.warn('Tooltip update failed:', e); }
    }
}

function disposeGpuCard(wrapper) {
    if (!wrapper) return;
    if (wrapper._gpuTooltip) { try { wrapper._gpuTooltip.dispose(); } catch (e) { } }
    wrapper.remove();
}

function getMemoryUsedGB(gpu) {
    // null = 后端解析失败(N/A),显示 "-" 而不是伪装成 0
    if (gpu?.['memory.used'] == null) return '-';
    const v = Number(gpu['memory.used']);
    return (Number.isFinite(v) && v > 0) ? (v / 1024).toFixed(1) : '0.0';
}

function getMemoryTotalGB(gpu) {
    if (gpu?.['memory.total'] == null) return '-';
    const v = Number(gpu['memory.total']);
    return (Number.isFinite(v) && v > 0) ? (v / 1024).toFixed(0) : '0';
}

function safeId(value) { return String(value).replace(/[^a-zA-Z0-9_-]/g, '_'); }

function buildTooltipContent(node, gpu) {
    return `<div class="text-start small">${escapeHtml(gpu.user_processes || gpu.users || 'None')}</div>`;
}

// ===== Table 逻辑 =====
function updateTable(gpustats) {
    if (!dataTable) return;
    const newData = [];
    (gpustats || []).forEach(function (node) {
        const gpus = Array.isArray(node.gpus) ? node.gpus : [];
        if (gpus.length > 0) {
            const nodeLabel = node.alias ? `${node.alias} (${node.hostname})` : node.hostname;
            gpus.forEach(function (gpu) {
                const util = Number(gpu['utilization.gpu']) || 0;
                newData.push([
                    escapeHtml(nodeLabel),
                    `[${escapeHtml(String(gpu.index || ''))}] ${escapeHtml(gpu.name || '')}`,
                    `${escapeHtml(String(gpu['temperature.gpu'] || '-'))}°C`,
                    `<div class="progress" style="height:20px;position:relative;"><div class="progress-bar bg-success" role="progressbar" style="width:${Math.max(0, Math.min(100, util))}%"></div><span style="position:absolute;width:100%;text-align:center;color:black;font-size:0.8rem;line-height:20px;">${escapeHtml(String(gpu['utilization.gpu'] || '-'))}%</span></div>`,
                    `${escapeHtml(String(gpu.memory || 0))}% <span class="text-muted small">(${escapeHtml(String(gpu['memory.used'] ?? '-'))} / ${escapeHtml(String(gpu['memory.total'] ?? '-'))})</span>`,
                    `${escapeHtml(String(gpu['power.draw'] || '-'))} / ${escapeHtml(String(gpu['enforced.power.limit'] || '-'))} W`,
                    `<span class="text-truncate d-block" style="max-width:400px;" title="${escapeHtml(gpu.user_processes || gpu.users || '')}">${escapeHtml(gpu.user_processes || gpu.users || '')}</span>`
                ]);
            });
        } else if (node.error) {
            newData.push([
                escapeHtml(node.alias ? `${node.alias} (${node.hostname})` : node.hostname),
                `<span class="text-danger"><i class="fas fa-exclamation-triangle"></i> Error</span>`,
                '-', '-', '-', '-',
                `<span class="text-danger small">${escapeHtml(node.error)}</span>`
            ]);
        }
    });
    dataTable.clear();
    dataTable.rows.add(newData);
    dataTable.draw(false);
}

// ===== 弹窗及管理逻辑 =====
let menubarServersMap = {};  // hostname -> 是否在菜单栏显示(缺省 true)

async function loadAppSettings() {
    try {
        const res = await fetch('/api/admin/settings', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        menubarServersMap = (data && data.menubar_servers) || {};
    } catch (e) {
        console.error('Load app settings failed', e);
    }
}

async function saveMenubarServer(hostname, enabled) {
    try {
        const res = await fetch('/api/admin/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ menubar_servers: { [hostname]: enabled } })
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        menubarServersMap[hostname] = enabled;
    } catch (e) {
        console.error('Save menubar setting failed', e);
        alert('Failed to save menu bar setting');
    }
}

async function loadSettings() {
    await loadAppSettings();

    const tbody = document.getElementById('server-list-body');
    tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">Loading...</td></tr>';
    try {
        const res = await fetch('/api/admin/servers', { cache: 'no-store' });
        if (!res.ok) throw new Error('Failed to load servers');
        const servers = await res.json();
        
        if (sortableInstance) {
            sortableInstance.destroy();
            sortableInstance = null;
        }
        
        tbody.innerHTML = '';

        if (selectedServer !== null && !servers.some(s => s.hostname === selectedServer)) {
            selectedServer = null;
        }
        renderServerShortcuts(servers);

        if (servers.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-3">No servers. Add one above.</td></tr>';
            return;
        }

        servers.forEach(function (s) {
            tbody.insertAdjacentHTML('beforeend', `
                <tr data-host="${escapeHtml(s.hostname)}">
                    <td class="text-center align-middle"><i class="fas fa-bars cursor-move handle px-2" title="Drag to reorder"></i></td>
                    <td class="fw-bold align-middle">${escapeHtml(s.hostname)}</td>
                    <td class="align-middle" style="min-width:140px; max-width:200px;">
                        <input type="text" class="form-control form-control-sm alias-input" value="${escapeAttr(s.alias || '')}" placeholder="e.g. lab-a100" maxlength="64" title="编辑后按回车或点击别处保存">
                    </td>
                    <td class="align-middle">${escapeHtml(String(s.port))}</td>
                    <td class="align-middle"><span class="badge bg-secondary text-light">${escapeHtml(s.username)}</span></td>
                    <td class="text-center align-middle">
                        <div class="form-check form-switch d-inline-block m-0">
                            <input class="form-check-input menubar-switch" type="checkbox" ${menubarServersMap[s.hostname] !== false ? 'checked' : ''} title="Show this server in the macOS menu bar">
                        </div>
                    </td>
                    <td class="text-end align-middle"><button class="btn btn-outline-danger btn-sm" onclick="deleteServer('${escapeJsString(s.hostname)}')"><i class="fas fa-trash-alt"></i></button></td>
                </tr>
            `);
        });

        // 菜单栏逐台开关:切换后由后端 3 秒内增删对应状态项
        tbody.querySelectorAll('.menubar-switch').forEach(function (input) {
            input.addEventListener('change', function () {
                const host = input.closest('tr')?.getAttribute('data-host');
                if (host) saveMenubarServer(host, input.checked);
            });
        });

        // 别名行内编辑：失焦或回车保存
        tbody.querySelectorAll('.alias-input').forEach(function (input) {
            input.addEventListener('change', async function () {
                const host = input.closest('tr')?.getAttribute('data-host');
                if (!host) return;
                const newAlias = input.value.trim();
                try {
                    const res = await fetch('/api/admin/servers/rename', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ hostname: host, alias: newAlias })
                    });
                    if (!res.ok) {
                        const data = await res.json().catch(() => ({}));
                        alert('Failed to save alias: ' + (data.error || res.status));
                        return;
                    }
                    input.value = newAlias;
                    await loadServerShortcuts();
                    fetchData();
                } catch (e) {
                    console.error('Save alias failed', e);
                    alert('Connection error');
                }
            });
        });

        // 设置列表的拖拽也同步主界面卡片
        sortableInstance = new Sortable(tbody, {
            handle: '.handle',
            animation: 150,
            ghostClass: 'sortable-ghost',
            onChange: function() {
                const rows = document.querySelectorAll('#server-list-body tr');
                const hostnames = Array.from(rows).map(row => row.getAttribute('data-host')).filter(Boolean);
                reorderGpuCardsByHostnames(hostnames);
            },
            onEnd: async function () { 
                const rows = document.querySelectorAll('#server-list-body tr');
                const hostnames = Array.from(rows).map(row => row.getAttribute('data-host')).filter(Boolean);
                reorderGpuCardsByHostnames(hostnames);
                await saveOrder(); 
            }
        });
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" class="text-danger">Error: ${escapeHtml(String(e))}</td></tr>`;
    }
}

async function saveOrder() {
    const rows = document.querySelectorAll('#server-list-body tr');
    const newOrder = Array.from(rows).map(row => row.getAttribute('data-host')).filter(Boolean);
    try {
        await fetch('/api/admin/servers/reorder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(newOrder)
        });
        await loadServerShortcuts();
        fetchData();
    } catch (e) {
        console.error('Save order failed', e);
        alert('Failed to save sort order');
        await loadSettings(); 
    }
}

async function addServer() {
    const btn = document.querySelector('#add-server-form button');
    const originalHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

    const payload = {
        hostname: document.getElementById('new-host').value.trim(),
        port: document.getElementById('new-port').value,
        username: document.getElementById('new-user').value.trim(),
        password: document.getElementById('new-pass').value,
        alias: document.getElementById('new-alias').value.trim()
    };

    try {
        const res = await fetch('/api/admin/servers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (res.ok) {
            document.getElementById('add-server-form').reset();
            await loadSettings();
            await loadServerShortcuts();
            fetchData();
        } else {
            const data = await res.json();
            alert('Error: ' + (data.error || 'Unknown'));
        }
    } catch (e) {
        console.error(e);
        alert('Connection error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
    }
}

async function addServersBulk() {
    const btn = document.getElementById('bulk-add-btn');
    const originalHtml = btn.innerHTML;
    const textarea = document.getElementById('bulk-servers-textarea');
    const rawLines = textarea.value.trim().split(/\r?\n/).map(line => line.trim()).filter(Boolean);

    if (rawLines.length === 0) { alert('请先输入要添加的服务器。'); return; }

    const servers = [];
    rawLines.forEach(function (line) {
        const parts = line.split(',').map(part => part.trim());
        if (parts.length < 4 || parts.length > 5) return;
        const [hostname, port, username, password, alias] = parts;
        if (hostname && port && username && password) {
            const server = { hostname, port, username, password };
            if (alias) server.alias = alias;
            servers.push(server);
        }
    });

    if (servers.length === 0) {
        alert('未找到有效的服务器行，请确保每行格式为 hostname,port,username,password[,alias]。');
        return;
    }

    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Adding...';

    try {
        const res = await fetch('/api/admin/servers/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ servers })
        });
        const data = await res.json();
        if (res.ok) {
            let message = `成功添加 ${data.added} 台服务器。`;
            if (data.skipped && data.skipped.length) message += `\n已跳过重复主机: ${data.skipped.join(', ')}`;
            if (data.invalid) message += `\n无效条目: ${data.invalid}`;
            alert(message);
            textarea.value = '';
            await loadSettings();
            await loadServerShortcuts();
            fetchData();
        } else {
            alert('Error: ' + (data.error || 'Unknown'));
        }
    } catch (e) {
        console.error(e);
        alert('Connection error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
    }
}

window.deleteServer = async function (hostname) {
    if (!confirm(`Delete ${hostname}?`)) return;
    try {
        const response = await fetch('/api/admin/servers', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hostname })
        });
        if (!response.ok) throw new Error('Delete failed');
        if (selectedServer === hostname) selectedServer = null;
        await loadSettings();
        await loadServerShortcuts();
        fetchData();
    } catch (e) {
        console.error(e);
        alert('Error deleting');
    }
};

// ===== HTML / JS 转义 =====
function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
}

function escapeJsString(value) {
    return String(value).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"').replace(/\r/g, '\\r').replace(/\n/g, '\\n');
}
