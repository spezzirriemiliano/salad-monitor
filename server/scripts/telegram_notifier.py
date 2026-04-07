"""
Salad Monitor - Telegram Notifier
Formats and sends the consolidated status report to Telegram.
"""

from datetime import datetime, timezone, timedelta
from html import escape

import requests


def send_telegram_report(
    metrics_store: dict,
    config: dict,
    expected_machines: list,
    gpu_history: dict = None,
    earnings_cache: dict = None,
    earnings_history: dict = None,
    balance_cache: dict = None,
) -> bool:
    bot_token = config["telegram"]["token"]
    chat_id = config["telegram"]["chat_id"]
    stale_threshold = config["telegram"].get("stale_threshold_minutes", 5)

    message = format_report(
        metrics_store,
        expected_machines,
        stale_threshold,
        gpu_history or {},
        earnings_cache or {},
        earnings_history or {},
        balance_cache or {},
    )
    return send_message(bot_token, chat_id, message)


def _is_fresh(received_at: str, now: datetime, threshold_minutes: int) -> bool:
    if not received_at:
        return False
    try:
        received = datetime.fromisoformat(received_at)
        return (now - received) <= timedelta(minutes=threshold_minutes)
    except Exception:
        return False


def _e(value) -> str:
    return escape(str(value))


def _short_id(machine_id: str) -> str:
    if len(machine_id) <= 10:
        return machine_id
    return f"{machine_id[:4]}..{machine_id[-4:]}"


def _temp_icon(temp) -> str:
    if temp is None:
        return ""
    if temp >= 85:
        return "🔴"
    if temp >= 80:
        return "🟡"
    return ""


def format_report(
    metrics_store: dict,
    expected_machines: list,
    stale_threshold_minutes: int,
    gpu_history: dict = None,
    earnings_cache: dict = None,
    earnings_history: dict = None,
    balance_cache: dict = None,
) -> str:
    now = datetime.now(timezone.utc)
    lines = []

    # ── Header ──────────────────────────────────────────────────
    lines.append("<b>Salad Monitor</b>")
    lines.append("")

    # ── Machines ─────────────────────────────────────────────────
    all_ids = sorted(set(list(metrics_store.keys()) + list(expected_machines)))
    if not all_ids:
        all_ids = sorted(metrics_store.keys())

    active_count = 0
    earning_count = 0
    total_power_w = 0
    alert_hot_gpu   = []   # temp >= 80°C
    alert_no_earn   = []   # earning_rate < 0.001

    for machine_id in all_ids:
        m = metrics_store.get(machine_id)

        if m is None:
            lines.append(f"⚫ <code>{_short_id(machine_id):<10}</code> — never reported")
            continue

        received_at = m.get("received_at", "")
        stale = not _is_fresh(received_at, now, stale_threshold_minutes)
        last_seen_str = ""
        if stale and received_at:
            try:
                received = datetime.fromisoformat(received_at)
                mins_ago = int((now - received).total_seconds() / 60)
                last_seen_str = f" <i>({mins_ago}m ago)</i>"
            except Exception:
                pass

        salad_on = m.get("salad_running", False)

        earning_rate = (earnings_cache or {}).get(machine_id)
        no_earning = earning_rate is not None and earning_rate < 0.001

        if stale:
            icon = "🟡"
        elif salad_on:
            icon = "🟠" if no_earning else "🟢"
            active_count += 1
        else:
            icon = "🔴"

        # GPU info
        gpus = m.get("gpus", [])
        if isinstance(gpus, dict):
            gpus = [gpus] if gpus else []

        gpu_str = "GPU:N/A"
        if gpus:
            g = gpus[0]
            util = g.get("utilization_pct", 0)
            temp = g.get("temperature_c")
            fan  = g.get("fan_speed_pct")

            power_w = g.get("power_w")
            if power_w is not None:
                total_power_w += power_w

            temp_icon = _temp_icon(temp)
            temp_str  = f"T: {temp_icon}{temp}°C" if temp is not None else "T: —"
            fan_str   = f" | F: {fan}%" if fan is not None else ""
            gpu_str   = f"GPU - L: {util}% | {temp_str}{fan_str}"

            # Collect alerts
            fresh = _is_fresh(received_at, now, stale_threshold_minutes)
            if fresh and temp is not None and temp >= 80:
                alert_hot_gpu.append(machine_id)

        # Earning rate
        earn_str = ""
        if earning_rate is not None:
            earn_str = f"  💰 ${earning_rate:.2f}/hr"
            fresh = _is_fresh(received_at, now, stale_threshold_minutes)
            if fresh and not no_earning:
                earning_count += 1
            if fresh and earning_rate < 0.001:
                alert_no_earn.append(machine_id)

        salad_str = "ON " if salad_on else "OFF"
        line1 = f"{icon} <code>{_short_id(machine_id):<10}</code> {salad_str}{earn_str}{last_seen_str}"
        lines.append(line1)
        if gpus:
            lines.append(f"      {gpu_str}")

    # ── Summary ──────────────────────────────────────────────────
    lines.append("")
    lines.append("")

    if total_power_w > 0:
        lines.append(f"⚡ Total Power: <b>{round(total_power_w)}W</b>")

    current_balance = (balance_cache or {}).get("current")
    if current_balance is not None:
        lines.append(f"💵 Current Balance: <tg-spoiler><b>${current_balance:.2f}</b></tg-spoiler>")

    e24h = (earnings_history or {}).get("24h")
    e30d = (earnings_history or {}).get("30d")
    if e24h is not None or e30d is not None:
        parts = []
        if e24h is not None:
            parts.append(f"24h: <b>${e24h:.2f}</b>")
        if e30d is not None:
            parts.append(f"30d: <b>${e30d:.2f}</b>")
        lines.append("💰 " + "  |  ".join(parts))

    # Last 1h → Est. 24h (sum of current earning rates)
    rates = [r for r in (earnings_cache or {}).values() if r is not None]
    if rates:
        last_1h = sum(rates)
        est_24h = last_1h * 24
        lines.append(f"🔮 Last 1h: <b>${last_1h:.2f}</b>  →  Est. 24h: <b>${est_24h:.2f}</b>")

    total = len(all_ids)
    active_icon = "🟢" if active_count == total else ("🟡" if active_count > 0 else "🔴")
    earning_icon = "🟢" if earning_count == total else ("🟡" if earning_count > 0 else "🔴")
    lines.append(f"{active_icon} Active: <b>{active_count}/{total}</b>")
    lines.append(f"{earning_icon} Earning: <b>{earning_count}/{total}</b>")

    # ── Alerts ───────────────────────────────────────────────────
    alert_rows = []
    if alert_hot_gpu:
        pills = ", ".join(f"<code>{_e(mid)}</code>" for mid in alert_hot_gpu)
        alert_rows.append(f"🔥 <b>GPU Temp:</b> {pills}")
    if alert_no_earn:
        pills = ", ".join(f"<code>{_e(mid)}</code>" for mid in alert_no_earn)
        alert_rows.append(f"💸 <b>No Earning:</b> {pills}")

    if alert_rows:
        lines.append("")
        lines.append("<b>⚠️ Alerts:</b>")
        lines.extend(alert_rows)

    return "\n".join(lines)


def send_message(bot_token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] Telegram API error: {e} — {resp.text}")
        return False
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram message: {e}")
        return False
