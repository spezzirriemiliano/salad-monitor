"""
Salad Monitor - Central Server
Receives metrics from all agents and sends consolidated Telegram messages.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import socket
import time
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests as req_lib
from flask import Flask, request, jsonify, send_from_directory, make_response
from apscheduler.schedulers.background import BackgroundScheduler

from telegram_notifier import send_telegram_report

VERSION = "v0.5"
_DEV_CONFIG     = Path(__file__).parent.parent / ".dev.config.json"
_DEFAULT_CONFIG = Path(__file__).parent.parent / "config.json"
CONFIG_PATH = _DEV_CONFIG if _DEV_CONFIG.exists() else _DEFAULT_CONFIG

STATS_DIR = Path(__file__).parent.parent / "machines-stats"
STATS_DIR.mkdir(exist_ok=True)
STATS_RETENTION_HOURS = 24

# Per-machine file locks to avoid concurrent writes on the same file
_stats_locks: dict = {}
_stats_locks_mutex = threading.Lock()

def _get_stats_lock(machine_id: str) -> threading.Lock:
    with _stats_locks_mutex:
        if machine_id not in _stats_locks:
            _stats_locks[machine_id] = threading.Lock()
        return _stats_locks[machine_id]

def _bucket_ts(dt=None, seconds=60) -> str:
    """Round a datetime to the nearest bucket (default: 1 minute)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    ts = int(dt.timestamp())
    return datetime.fromtimestamp(round(ts / seconds) * seconds, tz=timezone.utc).isoformat()

app = Flask(__name__)

# Auth: active session tokens { token: expiry_timestamp }
active_sessions: dict = {}
sessions_lock = threading.Lock()
SESSION_MAX_AGE = 10 * 365 * 24 * 3600  # 10 years — effectively no expiry

# Thread-safe store: { machine_id: latest_metrics_dict }
metrics_store: dict = {}
store_lock = threading.Lock()

# GPU utilization history: { machine_id: deque([util1, util2, ...], maxlen=5) }
gpu_history: dict = {}
GPU_HISTORY_SIZE = 5
JOB_GPU_THRESHOLD = 30  # % promedio mínimo para considerar job activo

# Earnings cache: { machine_id: float }  (tasa horaria ya con multiplier)
earnings_cache: dict = {}
earnings_lock  = threading.Lock()

# Timestamp of the last earnings fetch triggered from /report (60s cooldown)
_last_earnings_trigger: float = 0.0
_earnings_trigger_lock = threading.Lock()

# Latest Salad version from GitHub
latest_salad_version: str = ""
latest_version_lock = threading.Lock()

# Earnings history: { "24h": float, "30d": float, "1h": float }
earnings_history: dict = {}
earnings_history_lock = threading.Lock()

# Raw 30-day daily earnings: { "2024-01-15": 0.50, ... }
earnings_history_raw: dict = {}
earnings_history_raw_lock = threading.Lock()

# Bandwidth history: { machine_id: deque([{ uploaded_mb, downloaded_mb, ts }]) }
bandwidth_history: dict = {}
bandwidth_history_lock = threading.Lock()
BW_HISTORY_MAXLEN = 1440  # 24h at 60s intervals

# Balance: { "current": float, "lifetime": float }
balance_cache: dict = {}
balance_lock = threading.Lock()

# Earnings history cache: { salad_machine_id: { "data": dict, "ts": float } }
_earnings_history_cache: dict = {}
_earnings_history_cache_lock = threading.Lock()
EARNINGS_HISTORY_CACHE_TTL = 30 * 60  # 30 minutes

# Telegram notifications toggle
telegram_enabled: bool = True
telegram_enabled_lock = threading.Lock()

# Expected machines (auto-populated as agents report in)
expected_machines: list = []
expected_machines_lock = threading.Lock()

# Salad API cookie status: "ok" | "not_configured" | "expired"
salad_cookie_status: str = "ok"
salad_cookie_status_lock = threading.Lock()

SALAD_API_HEADERS = {
    "accept":       "application/json",
    "origin":       "https://salad.com",
    "x-xsrf-token": "1",
    "user-agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def fetch_latest_salad_version():
    """Fetch the latest Salad release tag from GitHub."""
    global latest_salad_version
    try:
        r = req_lib.get(
            "https://api.github.com/repos/SaladTechnologies/salad-applications/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "salad-monitor/1.0"},
            timeout=10,
        )
        if r.status_code == 200:
            tag = r.json().get("tag_name", "")
            with latest_version_lock:
                latest_salad_version = tag
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Latest Salad version: {tag}")
        else:
            print(f"[WARN] GitHub releases: HTTP {r.status_code}")
    except Exception as e:
        print(f"[WARN] fetch_latest_salad_version error: {e}")


def fetch_earnings_history():
    """Fetch 30-day history and calculate 24h and 30d totals."""
    salad_cfg = config.get("salad_api", {})
    auth = salad_cfg.get("auth_cookie", "")
    cf   = salad_cfg.get("cf_clearance", "")

    if not auth:
        return

    cookies = {"auth": auth}
    if cf:
        cookies["cf_clearance"] = cf

    try:
        r = req_lib.get(
            "https://app-api.salad.com/api/v2/reports/30-day-earning-history",
            cookies=cookies, headers=SALAD_API_HEADERS, timeout=15
        )
        if r.status_code != 200:
            print(f"[WARN] Earnings history: HTTP {r.status_code}")
            return

        data = r.json()
        now  = datetime.now(timezone.utc)
        cutoff_24h = now - timedelta(hours=24)
        cutoff_1h  = now - timedelta(hours=1)

        total_30d = 0.0
        total_24h = 0.0
        total_1h  = 0.0
        daily: dict = {}
        for ts_str, val in data.items():
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                total_30d += val
                if ts >= cutoff_24h:
                    total_24h += val
                if ts >= cutoff_1h:
                    total_1h += val
                # Accumulate by UTC date (key = "YYYY-MM-DD")
                day_key = ts.strftime("%Y-%m-%d")
                daily[day_key] = round(daily.get(day_key, 0.0) + val, 6)
            except Exception:
                continue

        with earnings_history_lock:
            earnings_history["1h"]  = round(total_1h,  4)
            earnings_history["24h"] = round(total_24h, 4)
            earnings_history["30d"] = round(total_30d, 4)

        with earnings_history_raw_lock:
            earnings_history_raw.clear()
            earnings_history_raw.update(daily)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Earnings history: 1h=${total_1h:.4f} 24h=${total_24h:.4f} 30d=${total_30d:.4f}")

    except Exception as e:
        print(f"[WARN] fetch_earnings_history error: {e}")


def fetch_balance():
    """Fetch current and lifetime balance from Salad API."""
    salad_cfg = config.get("salad_api", {})
    auth = salad_cfg.get("auth_cookie", "")
    cf   = salad_cfg.get("cf_clearance", "")

    if not auth:
        return

    cookies = {"auth": auth}
    if cf:
        cookies["cf_clearance"] = cf

    try:
        r = req_lib.get(
            "https://app-api.salad.com/api/v1/profile/balance",
            cookies=cookies, headers=SALAD_API_HEADERS, timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            with balance_lock:
                balance_cache["current"]  = data.get("currentBalance")
                balance_cache["lifetime"] = data.get("lifetimeBalance")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Balance: current=${data.get('currentBalance', 0):.2f} lifetime=${data.get('lifetimeBalance', 0):.2f}")
        elif r.status_code == 401:
            with salad_cookie_status_lock:
                global salad_cookie_status
                salad_cookie_status = "expired"
            print(f"\033[91m[WARN] Salad cookie expired (401). Run update_credentials.bat to update it.\033[0m")
        else:
            print(f"[WARN] Balance: HTTP {r.status_code}")
    except Exception as e:
        print(f"[WARN] fetch_balance error: {e}")


def fetch_salad_earnings():
    """Query the Salad API for each machine's earnings and cache the result."""
    salad_cfg = config.get("salad_api", {})
    auth      = salad_cfg.get("auth_cookie", "")
    cf        = salad_cfg.get("cf_clearance", "")
    mult      = 12

    if not auth:
        with salad_cookie_status_lock:
            global salad_cookie_status
            salad_cookie_status = "not_configured"
        return

    # Use expected_machines as source of truth (filtering those with a salad_machine_id)
    with expected_machines_lock:
        effective_map = {mid: sid for mid, sid in expected_machines.items() if sid}

    if not effective_map:
        return

    cookies = {"auth": auth}
    if cf:
        cookies["cf_clearance"] = cf

    ok = 0
    for mid, salad_id in effective_map.items():
        try:
            url = f"https://app-api.salad.com/api/v2/machines/{salad_id}/earnings/5-minutes"
            r   = req_lib.get(url, cookies=cookies, headers=SALAD_API_HEADERS, timeout=10)
            if r.status_code == 200:
                val = r.json()
                if isinstance(val, (int, float)):
                    with earnings_lock:
                        earnings_cache[mid] = round(float(val) * mult, 6)
                    with salad_cookie_status_lock:
                        salad_cookie_status = "ok"
                    ok += 1
            elif r.status_code == 401:
                print(f"\033[91m[WARN] Salad cookie expired (401). Run update_credentials.bat to update it.\033[0m")
                with salad_cookie_status_lock:
                    salad_cookie_status = "expired"
                break
            else:
                print(f"[WARN] Earnings {mid}: HTTP {r.status_code}")
        except Exception as e:
            print(f"[WARN] Earnings fetch error {mid}: {e}")

    if ok:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Earnings updated ({ok}/{len(effective_map)} machines)")


def is_job_active(machine_id: str, salad_running: bool) -> bool:
    if not salad_running:
        return False
    history = gpu_history.get(machine_id)
    if not history:
        return False
    avg = sum(history) / len(history)
    return avg >= JOB_GPU_THRESHOLD


def _bw_totals(history_deque, now):
    """Sums uploaded/downloaded MB for the last 15m, 1h and 24h from a bandwidth deque."""
    up_15m = dn_15m = up_1h = dn_1h = up_24h = dn_24h = 0.0
    cutoff_15m = now - timedelta(minutes=15)
    cutoff_1h  = now - timedelta(hours=1)
    cutoff_24h = now - timedelta(hours=24)
    for entry in history_deque:
        try:
            ts = datetime.fromisoformat(entry["ts"])
            up = entry["uploaded_mb"]
            dn = entry["downloaded_mb"]
            if ts >= cutoff_24h:
                up_24h += up; dn_24h += dn
            if ts >= cutoff_1h:
                up_1h  += up; dn_1h  += dn
            if ts >= cutoff_15m:
                up_15m += up; dn_15m += dn
        except Exception:
            pass
    return {
        "15m": {"uploaded_mb": round(up_15m, 1), "downloaded_mb": round(dn_15m, 1)},
        "1h":  {"uploaded_mb": round(up_1h,  1), "downloaded_mb": round(dn_1h,  1)},
        "24h": {"uploaded_mb": round(up_24h, 1), "downloaded_mb": round(dn_24h, 1)},
    }


_SAFE_ID = re.compile(r'^[\w\-]{1,64}$')

def _safe_machine_id(machine_id: str) -> str | None:
    """Return machine_id only if it's safe to use as a filename, else None."""
    return machine_id if _SAFE_ID.match(machine_id) else None


def append_machine_stat(machine_id: str, data: dict):
    """Append one bucketed stat entry to machines-stats/{machine_id}.json (NDJSON).
    Entries older than STATS_RETENTION_HOURS are dropped on each write.
    """
    if not _safe_machine_id(machine_id):
        return
    bw   = data.get("bandwidth") or {}
    gpus = data.get("gpus", [])
    if isinstance(gpus, dict):
        gpus = [gpus] if gpus else []

    gpu_entries = []
    for g in gpus:
        mem_used_mb  = g.get("memory_used_mb")
        mem_total_mb = g.get("memory_total_mb")
        gpu_entries.append({
            "util":      g.get("utilization_pct"),
            "temp":      g.get("temperature_c"),
            "hotspot":   g.get("hotspot_c"),
            "mem_temp":  g.get("memory_junction_c") if g.get("memory_junction_c") is not None else g.get("memory_temperature_c"),
            "mem_pct":   g.get("memory_utilization_pct"),
            "mem_gb":    round(mem_used_mb  / 1024, 2) if mem_used_mb  is not None else None,
            "mem_total_gb": round(mem_total_mb / 1024, 2) if mem_total_mb is not None else None,
            "power":     g.get("power_w"),
            "fan":       g.get("fan_speed_pct"),
        })

    ram_used_gb  = data.get("ram_used_gb")
    ram_total_gb = data.get("ram_total_gb")
    entry = {
        "ts":           _bucket_ts(),
        "cpu":          data.get("cpu_pct"),
        "ram_pct":      data.get("ram_used_pct"),
        "ram_gb":       round(ram_used_gb,  2) if ram_used_gb  is not None else None,
        "ram_total_gb": round(ram_total_gb, 2) if ram_total_gb is not None else None,
        "disk_pct": data.get("disk_used_pct"),
        "uptime_h": data.get("uptime_hours"),
        "mode":     data.get("mode"),
        "gpus":     gpu_entries,
        "up_mbps":  bw.get("upload_mbps"),
        "dn_mbps":  bw.get("download_mbps"),
    }

    path    = STATS_DIR / f"{machine_id}.json"
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=STATS_RETENTION_HOURS)
    lock    = _get_stats_lock(machine_id)

    with lock:
        lines = []
        last_ts = None
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        rec_ts = datetime.fromisoformat(rec["ts"])
                        if rec_ts >= cutoff:
                            lines.append(line)
                            last_ts = rec_ts
                    except Exception:
                        pass

        # Border case: rounding near the 30s mark can cause a report to skip a
        # bucket (gap ~120s instead of ~60s). If detected, insert a bridge entry
        # for the missing bucket so the chart stays continuous.
        new_ts = datetime.fromisoformat(entry["ts"])
        if last_ts is not None:
            gap = (new_ts - last_ts).total_seconds()
            if 90 <= gap < 180:
                bridge = dict(entry)
                bridge["ts"] = (new_ts - timedelta(seconds=60)).isoformat()
                lines.append(json.dumps(bridge, separators=(",", ":")))

        lines.append(json.dumps(entry, separators=(",", ":")))

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def load_config():
    if not CONFIG_PATH.exists():
        print(f"[ERROR] config.json not found. Copy config.example.json and edit it.")
        raise SystemExit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


config = load_config()
API_KEY = config["server"]["api_key"]
REPORT_INTERVAL_MINUTES = config["telegram"].get("report_interval_minutes", 15)
STALE_THRESHOLD_MINUTES = config["telegram"].get("stale_threshold_minutes", 5)
telegram_enabled = config["telegram"].get("notifications_enabled", True)
expected_machines = dict(config.get("expected_machines", {}))


def check_api_key():
    key = request.headers.get("X-API-Key", "")
    return key == API_KEY


# ── Auth helpers ─────────────────────────────────────────────

def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, h = stored_hash.split(":", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except Exception:
        return False


def _is_authenticated() -> bool:
    token = request.cookies.get("sm_session")
    if not token:
        return False
    now = time.time()
    with sessions_lock:
        expiry = active_sessions.get(token)
    return expiry is not None and now < expiry


def _password_configured() -> bool:
    return bool(config.get("dashboard", {}).get("password_hash", "").strip())


def _require_auth():
    """Returns a 401 response if not authenticated, else None."""
    if not _is_authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/api/setup", methods=["POST"])
def setup_password():
    if _password_configured():
        return jsonify({"error": "Already configured"}), 403
    data = request.get_json(silent=True) or {}
    password = data.get("password", "").strip()
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    hashed = _hash_password(password)
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        cfg.setdefault("dashboard", {})["password_hash"] = hashed
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        config["dashboard"] = cfg["dashboard"]
    except Exception as e:
        return jsonify({"error": f"Could not save config: {e}"}), 500
    token = secrets.token_hex(32)
    expiry = time.time() + SESSION_MAX_AGE
    with sessions_lock:
        active_sessions[token] = expiry
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie("sm_session", token, httponly=True, max_age=SESSION_MAX_AGE)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Dashboard password set up")
    return resp


@app.route("/api/login", methods=["POST"])
def login():
    if not _password_configured():
        return jsonify({"error": "Not configured"}), 403
    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    stored = config.get("dashboard", {}).get("password_hash", "")
    if not _verify_password(password, stored):
        return jsonify({"error": "Invalid password"}), 401
    token = secrets.token_hex(32)
    expiry = time.time() + SESSION_MAX_AGE
    with sessions_lock:
        active_sessions[token] = expiry
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie("sm_session", token, httponly=True, max_age=SESSION_MAX_AGE)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Dashboard login from {request.remote_addr}")
    return resp


@app.route("/api/logout", methods=["POST"])
def logout():
    token = request.cookies.get("sm_session")
    if token:
        with sessions_lock:
            active_sessions.pop(token, None)
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie("sm_session", "", expires=0)
    return resp


@app.route("/api/auth_status", methods=["GET"])
def auth_status():
    return jsonify({
        "authenticated": _is_authenticated(),
        "setup_required": not _password_configured(),
    })


@app.route("/report", methods=["POST"])
def receive_report():
    if not check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "machine_id" not in data:
        return jsonify({"error": "Invalid payload"}), 400

    machine_id = data["machine_id"]
    client_ip  = request.remote_addr
    data["received_at"] = datetime.now(timezone.utc).isoformat()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] POST /report  {machine_id}  {client_ip}")

    # Update GPU utilization history
    gpus = data.get("gpus", [])
    if isinstance(gpus, dict):
        gpus = [gpus] if gpus else []
    gpu_util = gpus[0].get("utilization_pct") if gpus else None

    with store_lock:
        metrics_store[machine_id] = data
        if gpu_util is not None:
            if machine_id not in gpu_history:
                gpu_history[machine_id] = deque(maxlen=GPU_HISTORY_SIZE)
            gpu_history[machine_id].append(gpu_util)

    # Update bandwidth history
    bw = data.get("bandwidth") or {}
    interval_up = bw.get("interval_uploaded_mb")
    interval_dn = bw.get("interval_downloaded_mb")
    if interval_up is not None and interval_dn is not None:
        entry = {
            "uploaded_mb":   interval_up,
            "downloaded_mb": interval_dn,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        with bandwidth_history_lock:
            if machine_id not in bandwidth_history:
                bandwidth_history[machine_id] = deque(maxlen=BW_HISTORY_MAXLEN)
            bandwidth_history[machine_id].append(entry)

    # Auto-register new machines and update salad_machine_id if available
    salad_mid = data.get("salad_machine_id")
    with expected_machines_lock:
        is_new   = machine_id not in expected_machines
        changed  = salad_mid and expected_machines.get(machine_id) != salad_mid
        if is_new:
            expected_machines[machine_id] = salad_mid
            print(f"[{datetime.now().strftime('%H:%M:%S')}] New machine registered: {machine_id}")
        elif changed:
            expected_machines[machine_id] = salad_mid
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Salad ID updated for {machine_id}: {salad_mid}")
    if is_new or changed:
        threading.Thread(target=save_expected_machines, daemon=True).start()

    # If the machine sends a salad_machine_id and has no earnings yet, trigger an immediate fetch
    if data.get("salad_machine_id"):
        with earnings_lock:
            already_has_earnings = machine_id in earnings_cache
        if not already_has_earnings:
            global _last_earnings_trigger
            now_ts = time.time()
            with _earnings_trigger_lock:
                if now_ts - _last_earnings_trigger > 60:
                    _last_earnings_trigger = now_ts
                    threading.Thread(target=fetch_salad_earnings, daemon=True).start()

    threading.Thread(target=append_machine_stat, args=(machine_id, data), daemon=True).start()

    return jsonify({"status": "ok"}), 200



@app.route("/status", methods=["GET"])
def get_status():
    err = _require_auth()
    if err: return err
    with store_lock:
        snapshot = dict(metrics_store)

    now = datetime.now(timezone.utc)
    result = []
    for mid, m in sorted(snapshot.items()):
        ts = m.get("received_at", "")
        stale = False
        if ts:
            try:
                received = datetime.fromisoformat(ts)
                stale = (now - received) > timedelta(minutes=STALE_THRESHOLD_MINUTES)
            except Exception:
                pass
        result.append({
            "machine_id": mid,
            "salad_running": m.get("salad_running"),
            "stale": stale,
            "last_seen": ts,
        })

    return jsonify({"machines": result, "count": len(result)})


@app.route("/dashboard")
def dashboard():
    return send_from_directory(Path(__file__).parent, "dashboard.html")


@app.route("/machine")
def machine_page():
    return send_from_directory(Path(__file__).parent, "machine.html")


@app.route("/earnings")
def earnings_page():
    return send_from_directory(Path(__file__).parent, "earnings.html")


@app.route("/api/earnings-overview", methods=["GET"])
def api_earnings_overview():
    err = _require_auth()
    if err: return err

    with earnings_history_lock:
        h_snap = dict(earnings_history)
    with earnings_history_raw_lock:
        raw_snap = dict(earnings_history_raw)
    with balance_lock:
        b_snap = dict(balance_cache)
    with earnings_lock:
        e_snap = dict(earnings_cache)
    with store_lock:
        store_snap = dict(metrics_store)
    with expected_machines_lock:
        exp_snap = dict(expected_machines)

    now = datetime.now(timezone.utc)

    # Build sorted daily list
    daily = sorted(
        [{"date": k, "amount": round(v, 4)} for k, v in raw_snap.items()],
        key=lambda x: x["date"]
    )

    # Build per-machine list
    machines = []
    for mid, salad_id in exp_snap.items():
        m = store_snap.get(mid, {})
        received_at = m.get("received_at", "")
        status = "never"
        if received_at:
            try:
                received = datetime.fromisoformat(received_at)
                stale = (now - received) > timedelta(minutes=STALE_THRESHOLD_MINUTES)
                status = "stale" if stale else ("online" if m.get("salad_running") else "offline")
            except Exception:
                pass
        machines.append({
            "machine_id":       mid,
            "salad_machine_id": salad_id,
            "earning_rate":     e_snap.get(mid),
            "status":           status,
            "cpu_name":         m.get("cpu_name"),
            "gpus":             m.get("gpus", []),
        })

    est_next_24h = None
    h1 = h_snap.get("1h")
    if h1 is not None:
        est_next_24h = round(h1 * 24, 4)

    return jsonify({
        "earnings_1h":    h_snap.get("1h"),
        "earnings_24h":   h_snap.get("24h"),
        "earnings_30d":   h_snap.get("30d"),
        "est_next_24h":   est_next_24h,
        "balance_current":  b_snap.get("current"),
        "balance_lifetime": b_snap.get("lifetime"),
        "daily":    daily,
        "machines": machines,
    })


@app.route("/api/dashboard")
def api_dashboard():
    err = _require_auth()
    if err: return err
    with store_lock:
        snapshot = dict(metrics_store)

    now = datetime.now(timezone.utc)
    with expected_machines_lock:
        expected = list(expected_machines.keys())
    all_ids = sorted(set(list(snapshot.keys()) + list(expected)))

    with earnings_lock:
        e_snap = dict(earnings_cache)

    with bandwidth_history_lock:
        bw_hist_snap = {mid: list(h) for mid, h in bandwidth_history.items()}

    machines = []
    for mid in all_ids:
        m = snapshot.get(mid)
        if m is None:
            machines.append({"machine_id": mid, "status": "never", "earning_rate": e_snap.get(mid)})
            continue

        received_at = m.get("received_at", "")
        stale = False
        if received_at:
            try:
                received = datetime.fromisoformat(received_at)
                stale = (now - received) > timedelta(minutes=STALE_THRESHOLD_MINUTES)
            except Exception:
                pass

        salad_on = m.get("salad_running", False)
        job_on = is_job_active(mid, salad_on)
        history = gpu_history.get(mid)
        gpu_avg = round(sum(history) / len(history)) if history else None

        if stale:
            status = "stale"
        elif salad_on:
            status = "online"
        else:
            status = "offline"

        gpus = m.get("gpus", [])
        if isinstance(gpus, dict):
            gpus = [gpus] if gpus else []

        machines.append({
            "machine_id": mid,
            "status": status,
            "salad_running": salad_on,
            "job_active": job_on,
            "gpu_avg": gpu_avg,
            "mode": m.get("mode", "gpu"),
            "cpu_pct": m.get("cpu_pct"),
            "ram_used_pct": m.get("ram_used_pct"),
            "ram_used_gb": m.get("ram_used_gb"),
            "ram_total_gb": m.get("ram_total_gb"),
            "disk_used_pct": m.get("disk_used_pct"),
            "uptime_hours": m.get("uptime_hours"),
            "hostname": m.get("hostname"),
            "cpu_name": m.get("cpu_name"),
            "salad_version": m.get("salad_version"),
            "salad_machine_id": m.get("salad_machine_id"),
            "last_seen": received_at,
            "gpus": gpus,
            "bandwidth": m.get("bandwidth"),
            "bandwidth_totals": _bw_totals(bw_hist_snap[mid], now) if mid in bw_hist_snap else None,
            "earning_rate": e_snap.get(mid),
        })

    with earnings_history_lock:
        h_snap = dict(earnings_history)

    with latest_version_lock:
        latest_ver = latest_salad_version

    with balance_lock:
        b_snap = dict(balance_cache)

    tg_cfg = config.get("telegram", {})
    telegram_configured = bool(tg_cfg.get("token", "").strip() and tg_cfg.get("chat_id", "").strip())

    active = sum(1 for m in machines if m["status"] == "online")
    return jsonify({
        "machines": machines,
        "active_count": active,
        "total_count": len(machines),
        "server_time": now.isoformat(),
        "stale_threshold_minutes": STALE_THRESHOLD_MINUTES,
        "earnings_1h":  h_snap.get("1h"),
        "earnings_24h": h_snap.get("24h"),
        "earnings_30d": h_snap.get("30d"),
        "latest_salad_version": latest_ver or None,
        "balance_current":  b_snap.get("current"),
        "balance_lifetime": b_snap.get("lifetime"),
        "telegram_configured": telegram_configured,
        "telegram_enabled": telegram_enabled,
        "salad_cookie_status": salad_cookie_status,
    })


@app.route("/api/earnings/<salad_machine_id>", methods=["GET"])
def get_machine_earnings(salad_machine_id: str):
    err = _require_auth()
    if err: return err

    salad_cfg = config.get("salad_api", {})
    auth = salad_cfg.get("auth_cookie", "")
    cf   = salad_cfg.get("cf_clearance", "")

    if not auth:
        return jsonify({"error": "Salad API not configured"}), 503

    cookies = {"auth": auth}
    if cf:
        cookies["cf_clearance"] = cf

    # Return cached response if still fresh
    with _earnings_history_cache_lock:
        cached = _earnings_history_cache.get(salad_machine_id)
        if cached and (time.time() - cached["ts"]) < EARNINGS_HISTORY_CACHE_TTL:
            return jsonify(cached["data"])

    try:
        r = req_lib.get(
            f"https://app-api.salad.com/api/v2/machines/{salad_machine_id}/earning-history?timeframe=30d",
            cookies=cookies, headers=SALAD_API_HEADERS, timeout=15,
        )
        if r.status_code == 401:
            return jsonify({"error": "expired"}), 401
        if r.status_code != 200:
            return jsonify({"error": f"HTTP {r.status_code}"}), r.status_code
        data = r.json()
        with _earnings_history_cache_lock:
            _earnings_history_cache[salad_machine_id] = {"data": data, "ts": time.time()}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats/<machine_id>", methods=["GET"])
def get_machine_stats(machine_id: str):
    err = _require_auth()
    if err: return err

    if not _safe_machine_id(machine_id):
        return jsonify({"error": "Invalid machine_id"}), 400

    path = STATS_DIR / f"{machine_id}.json"
    if not path.exists():
        return jsonify([])

    cutoff = datetime.now(timezone.utc) - timedelta(hours=STATS_RETENTION_HOURS)
    records = []
    lock = _get_stats_lock(machine_id)
    with lock:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if datetime.fromisoformat(rec["ts"]) >= cutoff:
                        records.append(rec)
                except Exception:
                    pass

    return jsonify(records)


@app.route("/api/telegram", methods=["GET"])
def get_telegram_status():
    err = _require_auth()
    if err: return err
    with telegram_enabled_lock:
        return jsonify({"enabled": telegram_enabled})


def save_expected_machines():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        with expected_machines_lock:
            cfg["expected_machines"] = dict(sorted(expected_machines.items()))
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Could not save expected_machines to config: {e}")


def save_telegram_enabled(state: bool):
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        cfg["telegram"]["notifications_enabled"] = state
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] Could not save telegram state to config: {e}")


@app.route("/api/telegram", methods=["POST"])
def set_telegram_status():
    err = _require_auth()
    if err: return err
    global telegram_enabled
    data = request.get_json(silent=True) or {}
    with telegram_enabled_lock:
        telegram_enabled = bool(data.get("enabled", not telegram_enabled))
        state = telegram_enabled
    save_telegram_enabled(state)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Telegram notifications {'enabled' if state else 'disabled'}")
    return jsonify({"enabled": state})


@app.route("/send_now", methods=["POST"])
def trigger_send():
    """Manually trigger a Telegram report."""
    if not check_api_key():
        return jsonify({"error": "Unauthorized"}), 401
    threading.Thread(target=send_scheduled_report, daemon=True).start()
    return jsonify({"status": "report queued"}), 200


def send_scheduled_report():
    with telegram_enabled_lock:
        enabled = telegram_enabled
    if not enabled:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Telegram notifications disabled, skipping report")
        return
    with store_lock:
        snapshot = dict(metrics_store)
        history_snapshot = {mid: list(h) for mid, h in gpu_history.items()}
    with earnings_lock:
        earnings_snap = dict(earnings_cache)
    with earnings_history_lock:
        earnings_hist_snap = dict(earnings_history)
    with balance_lock:
        balance_snap = dict(balance_cache)
    if not snapshot:
        print("[INFO] No data yet, skipping Telegram report")
        return
    with expected_machines_lock:
        expected = list(expected_machines.keys())
    ok = send_telegram_report(snapshot, config, expected, history_snapshot, earnings_snap, earnings_hist_snap, balance_snap)
    status = "sent" if ok else "failed"
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Telegram report {status} ({len(snapshot)} machines)")


def main():
    host = "0.0.0.0"
    port = config["server"].get("port", 5000)

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        send_scheduled_report,
        "interval",
        minutes=REPORT_INTERVAL_MINUTES,
        id="telegram_report",
    )
    scheduler.add_job(fetch_salad_earnings,        "interval", minutes=5,  id="salad_earnings")
    scheduler.add_job(fetch_earnings_history,      "interval", minutes=30, id="salad_history")
    scheduler.add_job(fetch_latest_salad_version,  "interval", minutes=30, id="salad_version")
    scheduler.add_job(fetch_balance,               "interval", minutes=10, id="salad_balance")
    scheduler.start()
    threading.Thread(target=fetch_salad_earnings,       daemon=True).start()
    threading.Thread(target=fetch_earnings_history,     daemon=True).start()
    threading.Thread(target=fetch_latest_salad_version, daemon=True).start()
    threading.Thread(target=fetch_balance,              daemon=True).start()

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = host

    GREEN = "\033[92m"
    RED   = "\033[91m"
    RESET = "\033[0m"
    print(f"{GREEN}[INFO] Salad Monitor Server {VERSION} starting on {local_ip}:{port}{RESET}")
    print(f"[INFO] Telegram reports every {REPORT_INTERVAL_MINUTES} minutes")

    auth_cookie = config.get("salad_api", {}).get("auth_cookie", "")
    if not auth_cookie:
        print(f"{RED}[WARN] Salad cookie not configured. Run update_credentials.bat to set it up.{RESET}")
    else:
        cf_clearance = config.get("salad_api", {}).get("cf_clearance", "")
        if not cf_clearance:
            print(f"{RED}[WARN] Salad cookie may be expired (cf_clearance missing). Run update_credentials.bat.{RESET}")

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host=host, port=port, debug=False, use_reloader=False)


def check_for_update():
    GREEN = "\033[92m"
    RESET = "\033[0m"
    try:
        url = "https://raw.githubusercontent.com/spezzirriemiliano/salad-monitor/main/server/scripts/server.py"
        r = req_lib.get(url, timeout=8)
        m = re.search(r'VERSION\s*=\s*"(v[^"]+)"', r.text)
        if not m:
            return
        remote = m.group(1)
        if remote != VERSION:
            print(f"{GREEN}[UPDATE] New version available: {remote} — run server_self_update.bat to update.{RESET}")
    except Exception:
        pass


if __name__ == "__main__":
    check_for_update()
    main()
