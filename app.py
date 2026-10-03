import json
import os
import time
import threading
import uuid
import paramiko
from flask import Flask, render_template, request, jsonify
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)



# ================= 配置与全局变量 =================
CONFIG_FILE = 'servers.json'
SECRETS_FILE = 'secrets.json'
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


if __name__ == '__main__':
    monitor_thread = threading.Thread(target=background_monitor_loop, daemon=True)
    monitor_thread.start()
    print(f"Server started on port 8888. Configuration loaded from {CONFIG_FILE} + {SECRETS_FILE}")
    app.run(debug=False, host='0.0.0.0', port=8888)