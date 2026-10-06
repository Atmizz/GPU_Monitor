import json
import os
import sys
import time
import threading
import uuid
import webbrowser
import paramiko
from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor

try:
    import rumps  # macOS 状态栏;非 Mac 环境缺依赖时自动降级为纯 Web 服务
except ImportError:
    rumps = None

app = Flask(__name__)



# ================= 配置与全局变量 =================
BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
PANEL_PORT = 18888  # 面板 Web 端口
CONFIG_FILE = 'servers.json'
SECRETS_FILE = 'secrets.json'
SETTINGS_FILE = 'settings.json'
# 持久化时需要剥离到 secrets.json 的敏感字段,servers.json 中不应出现
SENSITIVE_KEYS = ('hostname', 'port', 'username', 'password')

# 采集节奏自适应:有人看面板时全速轮询,无人看时降频,减少对节点的 SSH 压力
POLL_INTERVAL_ACTIVE = 1.0   # 有客户端在线时的采集间隔(秒)
POLL_INTERVAL_IDLE = 30.0    # 无客户端在线时的休眠采集间隔(秒)
CLIENT_ACTIVE_WINDOW = 10.0  # 距上次客户端拉取数据在该窗口内视为「有人在看」
CACHE_STALE_SECONDS = 2.0    # 缓存超过该秒数视为过期,客户端请求会唤醒采集线程立即刷新

SERVERS = []
SERVERS_LOCK = threading.Lock()

SSH_CLIENTS = {}
SSH_LOCK = threading.Lock()

GLOBAL_GPU_STATS = []
CACHE_LOCK = threading.Lock()
CACHE_UPDATED_AT = 0.0          # 缓存最近一次更新时间(判断缓存是否过期)
LAST_CLIENT_REQUEST = 0.0       # 最近一次客户端拉取数据的时间(判断是否有人在看)
WAKE_EVENT = threading.Event()  # 空闲降频期间客户端请求到达时唤醒采集线程

APP_SETTINGS = {'menubar_servers': {}}  # 每台服务器的菜单栏开关(缺省视为开启);全部关闭时回退为柱状图标


# ================= 持久化存储逻辑 =================
def load_secrets():
    """读取密码等敏感信息(按 hostname 索引),文件不存在时返回空字典。"""
    if not os.path.exists(SECRETS_FILE):
        return {}
    try:
        with open(SECRETS_FILE, 'r') as f:
            secrets = json.load(f)
        return secrets if isinstance(secrets, dict) else {}
    except Exception as e:
        print(f"Error loading secrets: {e}")
        return {}


def save_secrets(secrets):
    try:
        with open(SECRETS_FILE, 'w') as f:
            json.dump(secrets, f, indent=4)
    except Exception as e:
        print(f"Error saving secrets: {e}")


def load_app_settings():
    """读取全局设置;文件缺失或字段损坏时回退默认值。"""
    global APP_SETTINGS
    settings = {'menubar_servers': {}}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get('menubar_servers'), dict):
                settings['menubar_servers'] = {
                    str(host): bool(enabled)
                    for host, enabled in data['menubar_servers'].items()
                }
        except Exception as e:
            print(f"Error loading settings: {e}")
    APP_SETTINGS = settings


def save_app_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(APP_SETTINGS, f, indent=4)
    except Exception as e:
        print(f"Error saving settings: {e}")


def load_config():
    global SERVERS
    secrets = load_secrets()
    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump([], f)
            SERVERS = []
        except Exception as e:
            print(f"Error creating config file: {e}")
    else:
        try:
            with open(CONFIG_FILE, 'r') as f:
                SERVERS = json.load(f)
        except Exception as e:
            print(f"Error loading config: {e}")
            SERVERS = []

    # 敏感信息(hostname/port/password)只存放在 secrets.json,加载时按 id 合并回内存
    for server in SERVERS:
        if not isinstance(server, dict):
            continue
        if not server.get('id'):
            server['id'] = uuid.uuid4().hex[:12]
        secret = secrets.get(server['id'], {})
        if isinstance(secret, dict):
            for key in SENSITIVE_KEYS:
                if secret.get(key):
                    server[key] = secret[key]


def save_config():
    with SERVERS_LOCK:
        secrets = load_secrets()
        safe_servers = []
        for server in SERVERS:
            sid = server.get('id') or uuid.uuid4().hex[:12]
            entry = {k: v for k, v in server.items() if k not in SENSITIVE_KEYS}
            entry['id'] = sid
            secret = secrets.get(sid, {})
            for key in SENSITIVE_KEYS:
                if server.get(key):
                    secret[key] = server[key]
            secrets[sid] = secret
            safe_servers.append(entry)
        # servers.json 只落非敏感字段,hostname/port/password 单独写入 secrets.json
        save_secrets(secrets)
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(safe_servers, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")


load_config()
load_app_settings()

# ================= 命令定义 =================
SEPARATOR = "|||SECTION_SPLIT|||"
NVIDIA_SMI_GPU_FIELDS = (
    'uuid', 'index', 'name', 'temperature.gpu', 'utilization.gpu',
    'memory.used', 'memory.total', 'power.draw', 'power.limit'
)
CMD_GPU = f'nvidia-smi --query-gpu={",".join(NVIDIA_SMI_GPU_FIELDS)} --format=csv,noheader,nounits'
NVIDIA_SMI_PROC_FIELDS = ('gpu_uuid', 'pid', 'process_name', 'used_gpu_memory')
CMD_PROC = f'nvidia-smi --query-compute-apps={",".join(NVIDIA_SMI_PROC_FIELDS)} --format=csv,noheader,nounits'
COMBINED_CMD = f"{CMD_GPU} ; echo '{SEPARATOR}' ; {CMD_PROC}"


# ================= 核心逻辑 =================
def get_ssh_client(host_details):
    hostname = host_details['hostname']

    with SSH_LOCK:
        client = SSH_CLIENTS.get(hostname)
        if client and client.get_transport() and client.get_transport().is_active():
            return client

        if hostname in SSH_CLIENTS:
            try:
                SSH_CLIENTS[hostname].close()
            except:
                pass
            SSH_CLIENTS.pop(hostname, None)

    retries = 1
    last_error = None

    for attempt in range(retries):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            client.connect(
                hostname,
                port=int(host_details.get('port', 22)),
                username=host_details.get('username', ''),
                password=host_details.get('password', ''),
                timeout=5,
                banner_timeout=3,
                auth_timeout=3,
                look_for_keys=False,
                allow_agent=False
            )
            client.get_transport().set_keepalive(30)

            with SSH_LOCK:
                SSH_CLIENTS[hostname] = client
            return client

        except Exception as e:
            last_error = e
            try:
                client.close()
            except:
                pass
            time.sleep(1 + attempt)

    raise last_error


def fetch_single_server_data(host_details):
    hostname = host_details.get('hostname')
    if not hostname:
        # secrets.json 中缺少该 id 对应的 hostname/port 时不要让监控线程崩溃
        return {"hostname": None, "alias": host_details.get('alias', ''),
                "error": "Missing hostname (check secrets.json)", "gpus": []}
    alias = host_details.get('alias', '')
    try:
        client = get_ssh_client(host_details)

        stdin, stdout, stderr = client.exec_command(COMBINED_CMD, timeout=15)
        output = stdout.read().decode('utf-8').strip()

        if not output:
            return {"hostname": hostname, "alias": alias, "error": "Empty response"}

        parts = output.split(SEPARATOR)
        gpu_lines = parts[0].strip().splitlines() if len(parts) > 0 else []
        proc_lines = parts[1].strip().splitlines() if len(parts) > 1 else []

        gpus = []
        for line in gpu_lines:
            if not line.strip(): continue
            vals = [v.strip() for v in line.split(',')]
            if len(vals) < len(NVIDIA_SMI_GPU_FIELDS): continue
            gpus.append(dict(zip(NVIDIA_SMI_GPU_FIELDS, vals)))

        if not gpus:
            return {"hostname": hostname, "alias": alias, "error": "No GPUs found"}

        processes_by_uuid = {}
        all_pids = set()

        for line in proc_lines:
            if not line.strip(): continue
            vals = [v.strip() for v in line.split(',')]
            p_info = dict(zip(NVIDIA_SMI_PROC_FIELDS, vals))
            uuid = p_info['gpu_uuid']
            if uuid not in processes_by_uuid: processes_by_uuid[uuid] = []
            processes_by_uuid[uuid].append(p_info)
            all_pids.add(p_info['pid'])

        pid_to_user = {}
        if all_pids:
            pids_str = ",".join(all_pids)
            try:
                cmd_ps = f"ps -o pid=,user= -p {pids_str}"
                stdin, stdout, stderr = client.exec_command(cmd_ps, timeout=10)
                ps_out = stdout.read().decode('utf-8').strip()
                for line in ps_out.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        pid_to_user[parts[0]] = parts[1]
            except Exception:
                pass

        final_gpu_list = []
        for gpu in gpus:
            try:
                mem_used = int(float(gpu['memory.used']))
                mem_total = int(float(gpu['memory.total']))
                temp = int(float(gpu['temperature.gpu']))
                util = int(float(gpu['utilization.gpu']))
                power_draw = int(float(gpu['power.draw']))
                power_limit = int(float(gpu['power.limit']))
            except ValueError:
                mem_used = mem_total = temp = util = power_draw = power_limit = 0

            procs = processes_by_uuid.get(gpu['uuid'], [])
            proc_strs = []
            for p in procs:
                pid = p['pid']
                user = pid_to_user.get(pid, 'unknown')
                try:
                    mem = int(float(p['used_gpu_memory']))
                except ValueError:
                    mem = 0
                name = p['process_name'].replace(' ', '')
                proc_strs.append(f"{user}({name},{mem}M)")

            final_gpu_list.append({
                "index": str(int(gpu['index'])),
                "name": gpu['name'],
                "temperature.gpu": temp,
                "utilization.gpu": util,
                "memory.used": mem_used,
                "memory.total": mem_total,
                "memory": round((mem_used / mem_total) * 100) if mem_total > 0 else 0,
                "power.draw": power_draw,
                "enforced.power.limit": power_limit,
                "user_processes": " ".join(proc_strs),
                "users": len(proc_strs)
            })

        return {"hostname": hostname, "alias": alias, "gpus": final_gpu_list}

    except Exception as e:
        with SSH_LOCK:
            SSH_CLIENTS.pop(hostname, None)
        return {"hostname": hostname, "alias": alias, "error": str(e), "gpus": []}


def background_monitor_loop():
    global GLOBAL_GPU_STATS, CACHE_UPDATED_AT
    while True:
        start_time = time.time()
        with SERVERS_LOCK:
            current_servers = list(SERVERS)

        if current_servers:
            max_threads = min(10, len(current_servers))
            with ThreadPoolExecutor(max_workers=max_threads) as executor:
                results = list(executor.map(fetch_single_server_data, current_servers))
            with CACHE_LOCK:
                GLOBAL_GPU_STATS = results
                CACHE_UPDATED_AT = time.time()
        else:
            with CACHE_LOCK:
                GLOBAL_GPU_STATS = []

        # 近期有客户端拉取数据就全速轮询,否则降频休眠;
        # WAKE_EVENT 让空闲期的客户端请求能立即唤醒采集
        elapsed = time.time() - start_time
        if time.time() - LAST_CLIENT_REQUEST < CLIENT_ACTIVE_WINDOW:
            interval = POLL_INTERVAL_ACTIVE
        else:
            interval = POLL_INTERVAL_IDLE
        WAKE_EVENT.wait(max(0.0, interval - elapsed))
        WAKE_EVENT.clear()


# ================= Flask 路由 =================

@app.route('/')
def dashboard():
    return render_template('index.html')




@app.route('/api/gpustat/all')
def api_gpu_data():
    global LAST_CLIENT_REQUEST
    LAST_CLIENT_REQUEST = time.time()
    with CACHE_LOCK:
        stats = GLOBAL_GPU_STATS
        updated_at = CACHE_UPDATED_AT
    # 空闲降频期间缓存可能已过期:唤醒采集线程立刻刷新,本次请求先返回缓存数据
    if SERVERS and time.time() - updated_at > CACHE_STALE_SECONDS:
        WAKE_EVENT.set()
    return jsonify(stats)


# --- 管理接口 ---

@app.route('/api/admin/servers', methods=['GET'])
def get_servers():
    with SERVERS_LOCK:
        safe_list = []
        for s in SERVERS:
            safe_s = s.copy()
            if 'password' in safe_s:
                safe_s.pop('password')
            safe_list.append(safe_s)
        return jsonify(safe_list)


@app.route('/api/admin/servers', methods=['POST'])
def add_server():
    data = request.json
    required = ['hostname', 'port', 'username', 'password']
    if not all(k in data for k in required):
        return jsonify({"error": "Missing fields"}), 400

    entry = {
        'id': uuid.uuid4().hex[:12],
        'hostname': data['hostname'],
        'port': data['port'],
        'username': data['username'],
        'password': data['password']
    }
    alias = str(data.get('alias') or '').strip()
    if alias:
        entry['alias'] = alias

    with SERVERS_LOCK:
        for s in SERVERS:
            if s['hostname'] == data['hostname']:
                return jsonify({"error": "Hostname already exists"}), 400
        SERVERS.append(entry)

    save_config()
    WAKE_EVENT.set()  # 新节点立即参与采集,不必等空闲休眠结束
    return jsonify({"success": True})


@app.route('/api/admin/servers/bulk', methods=['POST'])
def add_servers_bulk():
    data = request.json
    if not isinstance(data, dict) or 'servers' not in data or not isinstance(data['servers'], list):
        return jsonify({"error": "Invalid payload"}), 400

    added = 0
    skipped = []
    invalid_count = 0
    with SERVERS_LOCK:
        existing_hosts = {s['hostname'] for s in SERVERS}
        for server in data['servers']:
            if not isinstance(server, dict):
                invalid_count += 1
                continue
            if not all(k in server for k in ['hostname', 'port', 'username', 'password']):
                invalid_count += 1
                continue
            hostname = server['hostname']
            if hostname in existing_hosts:
                skipped.append(hostname)
                continue
            existing_hosts.add(hostname)
            entry = {
                'id': uuid.uuid4().hex[:12],
                'hostname': hostname,
                'port': server['port'],
                'username': server['username'],
                'password': server['password']
            }
            alias = str(server.get('alias') or '').strip()
            if alias:
                entry['alias'] = alias
            SERVERS.append(entry)
            added += 1

    save_config()
    WAKE_EVENT.set()  # 新节点立即参与采集,不必等空闲休眠结束
    return jsonify({"success": True, "added": added, "skipped": skipped, "invalid": invalid_count})


@app.route('/api/admin/servers', methods=['DELETE'])
def delete_server():
    data = request.json
    hostname = data.get('hostname')

    with SERVERS_LOCK:
        global SERVERS
        SERVERS = [s for s in SERVERS if s['hostname'] != hostname]

    with SSH_LOCK:
        if hostname in SSH_CLIENTS:
            try:
                SSH_CLIENTS[hostname].close()
            except:
                pass
            SSH_CLIENTS.pop(hostname, None)

    save_config()
    APP_SETTINGS.get('menubar_servers', {}).pop(hostname, None)
    save_app_settings()
    WAKE_EVENT.set()  # 立即刷新缓存,移除已删除的节点
    return jsonify({"success": True})


@app.route('/api/admin/servers/reorder', methods=['POST'])
def reorder_servers():
    new_order_hostnames = request.json
    if not isinstance(new_order_hostnames, list):
        return jsonify({"error": "Invalid data format"}), 400

    with SERVERS_LOCK:
        global SERVERS
        server_map = {s['hostname']: s for s in SERVERS}
        new_servers_list = []
        seen_hosts = set()

        for hostname in new_order_hostnames:
            if hostname in server_map:
                new_servers_list.append(server_map[hostname])
                seen_hosts.add(hostname)

        for s in SERVERS:
            if s['hostname'] not in seen_hosts:
                new_servers_list.append(s)

        SERVERS = new_servers_list

    save_config()
    return jsonify({"success": True})


@app.route('/api/admin/servers/rename', methods=['POST'])
def rename_server():
    data = request.json or {}
    hostname = data.get('hostname')
    alias = str(data.get('alias') or '').strip()

    if not hostname:
        return jsonify({"error": "Missing hostname"}), 400
    if len(alias) > 64:
        return jsonify({"error": "Alias too long (max 64 chars)"}), 400

    with SERVERS_LOCK:
        target = next((s for s in SERVERS if s['hostname'] == hostname), None)
        if target is None:
            return jsonify({"error": "Server not found"}), 404
        if alias:
            target['alias'] = alias
        else:
            target.pop('alias', None)

    save_config()
    return jsonify({"success": True})


@app.route('/api/admin/settings', methods=['GET'])
def get_app_settings():
    return jsonify(APP_SETTINGS)


@app.route('/api/admin/settings', methods=['POST'])
def update_app_settings():
    data = request.json or {}
    if isinstance(data.get('menubar_servers'), dict):
        for host, enabled in data['menubar_servers'].items():
            APP_SETTINGS['menubar_servers'][str(host)] = bool(enabled)
    save_app_settings()
    return jsonify({"success": True, "settings": APP_SETTINGS})


# ================= macOS 状态栏 =================
def format_panel_stats():
    """汇总进程内缓存生成总览文字,只读内存,不产生任何 SSH 请求。"""
    with CACHE_LOCK:
        stats = list(GLOBAL_GPU_STATS)
    if stats:
        online = [s for s in stats if not s.get("error")]
        gpu_total = sum(len(s.get("gpus", [])) for s in online)
        utils = [g["utilization.gpu"] for s in online for g in s.get("gpus", [])]
        avg = round(sum(utils) / len(utils)) if utils else 0
        return f"{len(online)}/{len(stats)} 台在线 · {gpu_total} GPU · 平均 {avg}%"
    with SERVERS_LOCK:
        has_servers = bool(SERVERS)
    return "采集中…" if has_servers else "尚无节点，请打开面板添加"


if rumps is not None:
    from AppKit import (NSImage, NSFont, NSColor, NSAttributedString,
                        NSMutableParagraphStyle, NSBezierPath, NSStatusBar,
                        NSFontAttributeName, NSForegroundColorAttributeName,
                        NSParagraphStyleAttributeName, NSTextAlignmentCenter)
    from Foundation import NSMakeSize

    # 状态栏小方块排版(单位:点,渲染时 2 倍取样保证 Retina 清晰)
    BLOCK_W, BAR_H, SCALE = 26, 18, 2

    def _draw_text_centered(text, x, baseline_y, width, font_size, weight):
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(NSTextAlignmentCenter)
        attrs = {
            NSFontAttributeName: NSFont.monospacedDigitSystemFontOfSize_weight_(font_size, weight),
            NSForegroundColorAttributeName: NSColor.blackColor(),
            NSParagraphStyleAttributeName: para,
        }
        text = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        text.drawAtPoint_((x + (width - text.size().width) / 2, baseline_y))

    def render_server_block(stats_entry, index):
        """单个服务器的状态项图:上行 S*,下行 空闲数/GPU 总数,失败或无数据显示 --。"""
        if stats_entry.get("error") or not stats_entry.get("gpus"):
            bottom = "--"
        else:
            gpus = stats_entry["gpus"]
            free = sum(1 for g in gpus if g.get("users", 0) == 0)
            bottom = f"{free}/{len(gpus)}"
        img = NSImage.alloc().initWithSize_((BLOCK_W * SCALE, BAR_H * SCALE))
        img.lockFocus()
        _draw_text_centered(f"S{index}", 0, 18.8, BLOCK_W * SCALE, 8.2 * SCALE, 0.35)
        _draw_text_centered(bottom, 0, 1.6, BLOCK_W * SCALE, 8.2 * SCALE, 0.15)
        img.unlockFocus()
        img.setSize_((BLOCK_W, BAR_H))
        img.setTemplate_(True)
        return img

    def render_plain_icon():
        """状态栏显示关闭时的简洁图标:三根上升的负载柱,不带每服务器数据,菜单仍可打开。"""
        size = 18 * SCALE
        img = NSImage.alloc().initWithSize_((size, size))
        img.lockFocus()
        NSColor.blackColor().set()
        for x_pt, h_pt in ((2, 9), (7.5, 13), (13, 17)):
            NSBezierPath.bezierPathWithRect_(((x_pt * SCALE, 2), (3 * SCALE, h_pt * SCALE))).fill()
        img.unlockFocus()
        img.setSize_((size / SCALE, size / SCALE))
        img.setTemplate_(True)
        return img

    class GPUStatusbarApp(rumps.App):
        """菜单栏入口:状态栏逐服务器小块 + 下拉菜单(总览/节点明细/打开面板/退出)。

        状态栏图与菜单均来自采集线程维护的缓存,定时器只做展示刷新;
        退出项自行管理(quit_button=None),因为菜单每次动态重建会清掉自动注入的退出按钮。
        """

        REFRESH_SECONDS = 3

        def __init__(self):
            super().__init__("GPU Monitor", quit_button=None)
            self._open_item = rumps.MenuItem("打开面板", callback=self.open_dashboard, key="o")
            self._quit_item = rumps.MenuItem("退出", callback=self.quit_app, key="q")
            self._status_img = None
            self._server_items = {}  # hostname -> NSStatusItem(每台服务器一个独立状态项)
            self.refresh_menu()
            rumps.Timer(self.refresh_menu, self.REFRESH_SECONDS).start()

        def _apply_status_image(self):
            """把回退图标挂到 rumps 自身的状态项上。

            rumps 0.4.0 的 status item 挂在内部 delegate self._nsapp 上,run() 之后才存在;
            run() 之前先把图种到 self._icon_nsimage(rumps 初始化时读 _app['_icon_nsimage'],
            而 NSApp._app 正是本实例的 __dict__),启动瞬间即可显示。
            """
            if self._status_img is None:
                return
            self._icon_nsimage = self._status_img
            nsapp = getattr(self, '_nsapp', None)
            if nsapp is not None and hasattr(nsapp, 'nsstatusitem'):
                nsapp.nsstatusitem.setImage_(self._status_img)

        def _refresh_status_items(self, stats):
            """按逐台开关增删各服务器的独立状态项;全部关闭时回退为三根柱图标。

            rumps 自带的状态项平时隐藏,仅承担"全部关闭"时的回退展示;
            各服务器状态项与它共用同一个下拉菜单(总览/打开面板/退出始终可达)。
            """
            bar = NSStatusBar.systemStatusBar()
            enabled = APP_SETTINGS.get('menubar_servers', {})
            desired = {}
            for i, s in enumerate(stats, start=1):
                host = s.get("hostname")
                if host and enabled.get(host, True):
                    desired[host] = (i, s)
            for host in list(self._server_items):
                if host not in desired:
                    bar.removeStatusItem_(self._server_items.pop(host))
            for host, (i, s) in desired.items():
                item = self._server_items.get(host)
                if item is None:
                    item = bar.statusItemWithLength_(-1)
                    self._server_items[host] = item
                item.setImage_(render_server_block(s, i))
                item.setMenu_(self.menu._menu)
            self._status_img = render_plain_icon()
            self._apply_status_image()
            nsapp = getattr(self, '_nsapp', None)
            rumps_item = getattr(nsapp, 'nsstatusitem', None) if nsapp is not None else None
            if rumps_item is not None:
                rumps_item.setVisible_(not desired)

        def refresh_menu(self, _sender=None):
            with CACHE_LOCK:
                stats = list(GLOBAL_GPU_STATS)
            self._refresh_status_items(stats)
            self.menu.clear()
            self.menu.add(format_panel_stats())
            self.menu.add(rumps.separator)
            for i, s in enumerate(stats, start=1):
                name = s.get("alias") or s.get("hostname") or "未知节点"
                tag = f"S{i}"
                if s.get("error"):
                    self.menu.add(f"{tag} ⚠ {name} · 连接失败")
                else:
                    gpus = s.get("gpus", [])
                    if gpus:
                        util = round(sum(g["utilization.gpu"] for g in gpus) / len(gpus))
                        self.menu.add(f"{tag} · {name} · {len(gpus)} GPU · {util}%")
                    else:
                        self.menu.add(f"{tag} · {name} · 无 GPU 数据")
            self.menu.add(rumps.separator)
            self.menu.add(self._open_item)
            self.menu.add(self._quit_item)

        def open_dashboard(self, _sender):
            webbrowser.open(f"http://127.0.0.1:{PANEL_PORT}")

        def quit_app(self, _sender):
            rumps.quit_application()


if __name__ == '__main__':
    monitor_thread = threading.Thread(target=background_monitor_loop, daemon=True)
    monitor_thread.start()
    print(f"Server started on port {PANEL_PORT}. Configuration loaded from {CONFIG_FILE} + {SECRETS_FILE}")

    if rumps is None:
        # 无状态栏依赖的环境(如 Linux)保持原有前台 Web 服务行为
        app.run(debug=False, host='0.0.0.0', port=PANEL_PORT)
    else:
        # macOS:Flask 挪到后台线程,主线程运行状态栏事件循环
        threading.Thread(
            target=lambda: app.run(debug=False, host='0.0.0.0', port=PANEL_PORT),
            daemon=True,
        ).start()
        GPUStatusbarApp().run()