"""
Salad Monitor - Central Server
Receives metrics from all agents and sends consolidated Telegram messages.
"""

import json
import logging
import os
import socket
import time
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests as req_lib
from flask import Flask, request, jsonify, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler

from telegram_notifier import send_telegram_report

VERSION = "v0.1"
CONFIG_PATH = Path(__file__).parent.parent / "config.json"

app = Flask(__name__)

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

# Earnings history: { "24h": float, "30d": float }
earnings_history: dict = {}
earnings_history_lock = threading.Lock()

# Balance: { "current": float, "lifetime": float }
balance_cache: dict = {}
balance_lock = threading.Lock()

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
        for ts_str, val in data.items():
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                total_30d += val
                if ts >= cutoff_24h:
                    total_24h += val
                if ts >= cutoff_1h:
                    total_1h += val
            except Exception:
                continue

        with earnings_history_lock:
            earnings_history["1h"]  = round(total_1h,  4)
            earnings_history["24h"] = round(total_24h, 4)
            earnings_history["30d"] = round(total_30d, 4)

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

    return jsonify({"status": "ok"}), 200



@app.route("/status", methods=["GET"])
def get_status():
    """Quick web view of current state (optional, no auth for convenience)."""
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


@app.route("/api/dashboard")
def api_dashboard():
    with store_lock:
        snapshot = dict(metrics_store)

    now = datetime.now(timezone.utc)
    with expected_machines_lock:
        expected = list(expected_machines.keys())
    all_ids = sorted(set(list(snapshot.keys()) + list(expected)))

    with earnings_lock:
        e_snap = dict(earnings_cache)

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


@app.route("/api/telegram", methods=["GET"])
def get_telegram_status():
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
    scheduler.add_job(fetch_latest_salad_version,  "interval", hours=6,   id="salad_version")
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


if __name__ == "__main__":
    main()
