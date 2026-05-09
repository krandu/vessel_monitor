#!/usr/bin/env python3
"""
渔船靠港监控：粤珠渔养20003（MMSI: 412536814）
检测渔船返回洪湾渔港后推送 Telegram 通知
数据来源：shipinfo.net（免费，无需 API key）
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── 配置 ──────────────────────────────────────────────────────────────────────
VESSEL_MMSI   = "412536814"
VESSEL_NAME   = "粤珠渔养20003"

# 洪湾渔港中心坐标（珠海市斗门区，马骝洲水道北侧）
PORT_LAT      = 22.178
PORT_LON      = 113.437
PORT_NAME     = "洪湾渔港"

# 靠港判定条件
ARRIVE_RADIUS_KM  = 1.5   # 距港口中心 1.5km 以内
ARRIVE_MAX_SPEED  = 1.0   # 速度低于 1 节（基本停泊）

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# 状态文件
STATE_FILE = Path("vessel_state.json")

# shipinfo.net API
SHIPINFO_API = "https://shipinfo.net/topos/api/vessel/summary"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Referer": f"https://shipinfo.net/vessels_map_mmsi_{VESSEL_MMSI}",
}

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """计算两点间大圆距离（km）"""
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "in_port": False,
        "last_notified_arrival": None,
        "last_packet_time": None,
        "last_lat": None,
        "last_lon": None,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] 未配置 Telegram 凭据，跳过推送。")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        print("[OK] Telegram 消息发送成功")
        return True
    except Exception as e:
        print(f"[ERROR] Telegram 发送失败: {e}")
        return False


# ── 数据获取 ──────────────────────────────────────────────────────────────────

def fetch_vessel_position() -> dict | None:
    """
    从 shipinfo.net 获取船位数据。
    返回 {"lat", "lon", "speed", "course", "timestamp", "status"} 或 None。
    """
    params = {"imo": "0", "mmsi": VESSEL_MMSI}
    try:
        r = requests.get(
            SHIPINFO_API, params=params, headers=HEADERS, timeout=20
        )
        r.raise_for_status()
        data = r.json()
    except requests.exceptions.JSONDecodeError:
        # API 可能返回 HTML（未找到船只或需要登录），改爬网页
        return fetch_vessel_position_html()
    except Exception as e:
        print(f"[VESSEL] API 请求失败: {e}")
        return fetch_vessel_position_html()

    # shipinfo.net summary API 响应示例：
    # {"mmsi":..., "lat":22.17, "lon":113.43, "sog":0.0, "cog":..., "timestamp":"..."}
    try:
        lat = float(data.get("lat") or data.get("latitude") or 0)
        lon = float(data.get("lon") or data.get("longitude") or 0)
        speed = float(data.get("sog") or data.get("speed") or 0)
        course = float(data.get("cog") or data.get("course") or 0)
        ts = data.get("timestamp") or data.get("time") or ""
        if lat == 0 and lon == 0:
            raise ValueError("坐标为零，数据无效")
        return {"lat": lat, "lon": lon, "speed": speed,
                "course": course, "timestamp": ts}
    except Exception as e:
        print(f"[VESSEL] API 响应解析失败: {e}，原始数据: {data}")
        return fetch_vessel_position_html()


def fetch_vessel_position_html() -> dict | None:
    """备用方案：爬取 shipinfo.net 网页提取船位"""
    url = f"https://shipinfo.net/vessels_map_mmsi_{VESSEL_MMSI}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        text = r.text
    except Exception as e:
        print(f"[VESSEL] 网页抓取失败: {e}")
        return None

    import re

    # 提取坐标：匹配 "The current position of vessel ... is XX.XXXXX lat / XXX.XXXXX lng"
    coord_m = re.search(
        r"current position of vessel .+? is ([\d.]+) lat / ([\d.]+) lng",
        text, re.IGNORECASE
    )
    if not coord_m:
        # 尝试另一种格式："XX.XXXXX° N, XXX.XXXXX° E"
        coord_m = re.search(
            r"([\d.]+)°\s*N,\s*([\d.]+)°\s*E",
            text
        )
        if not coord_m:
            print(f"[VESSEL] 网页中未找到坐标，请检查页面是否变化。")
            return None

    lat = float(coord_m.group(1))
    lon = float(coord_m.group(2))

    # 提取最新 AIS 时间戳
    ts_m = re.search(
        r"Updated:\s*([\d\-]+ [\d:]+\s*UTC)",
        text
    )
    ts = ts_m.group(1).strip() if ts_m else ""

    # 提取速度（SOG）：匹配 "SOG X kn" 或 "speed: X"
    spd_m = re.search(r"SOG\s*([\d.]+)\s*kn", text)
    if not spd_m:
        spd_m = re.search(r"speed[:\s]+([\d.]+)", text, re.IGNORECASE)
    speed = float(spd_m.group(1)) if spd_m else 0.0

    print(f"[VESSEL] 网页解析成功: lat={lat}, lon={lon}, speed={speed}, ts={ts}")
    return {"lat": lat, "lon": lon, "speed": speed, "course": 0.0, "timestamp": ts}


# ── 主逻辑 ────────────────────────────────────────────────────────────────────

def check_vessel() -> None:
    print(f"\n[渔船] 开始检查 {VESSEL_NAME}（MMSI: {VESSEL_MMSI}）")

    pos = fetch_vessel_position()
    if pos is None:
        print("[渔船] 无法获取船位数据，本次跳过。")
        return

    lat, lon, speed, ts = pos["lat"], pos["lon"], pos["speed"], pos["timestamp"]
    dist_km = haversine_km(lat, lon, PORT_LAT, PORT_LON)

    print(f"[渔船] 位置: {lat:.5f}°N {lon:.5f}°E  速度: {speed:.1f}kn  "
          f"距{PORT_NAME}: {dist_km:.2f}km  时间: {ts}")

    state = load_state()
    last_ts = state.get("last_packet_time")

    # 判断是否靠港
    is_in_port = (dist_km <= ARRIVE_RADIUS_KM and speed <= ARRIVE_MAX_SPEED)
    was_in_port = state.get("in_port", False)

    if is_in_port and not was_in_port:
        # 新靠港事件：出海→回港
        print(f"[渔船] 检测到靠港！距港 {dist_km:.2f}km，速度 {speed:.1f}kn")

        # 格式化时间（尝试解析 UTC → UTC+8）
        try:
            # 支持 "2026-05-09 07:44:13 UTC" 格式
            dt = datetime.strptime(ts.replace(" UTC", "").strip(), "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
            ts_cst = dt.strftime("%Y-%m-%d %H:%M")
            ts_display = f"{ts_cst} UTC+8（{int((datetime.now(timezone.utc) - dt).total_seconds() / 60)}分钟前）"
        except Exception:
            ts_display = ts or "未知"

        msg = (
            f"⚓ <b>{VESSEL_NAME} 已返回{PORT_NAME}！</b>\n\n"
            f"🚢 MMSI: <code>{VESSEL_MMSI}</code>\n"
            f"📍 当前位置: {lat:.4f}°N, {lon:.4f}°E\n"
            f"🏠 距港口: {dist_km:.2f} km\n"
            f"⚡ 速度: {speed:.1f} 节\n"
            f"🕐 AIS 时间: {ts_display}\n\n"
            f"🔗 <a href=\"https://shipinfo.net/vessels_map_mmsi_{VESSEL_MMSI}\">查看实时位置</a>"
        )
        send_telegram(msg)
        state["last_notified_arrival"] = datetime.utcnow().isoformat()

    elif not is_in_port and was_in_port:
        # 离港事件
        print(f"[渔船] 检测到离港，距港 {dist_km:.2f}km，速度 {speed:.1f}kn")
        msg = (
            f"🌊 <b>{VESSEL_NAME} 已出海</b>\n\n"
            f"📍 当前位置: {lat:.4f}°N, {lon:.4f}°E\n"
            f"⚡ 速度: {speed:.1f} 节\n"
            f"🕐 AIS 时间: {ts or '未知'}"
        )
        send_telegram(msg)

    else:
        status = "靠泊中" if is_in_port else f"在海上（距港 {dist_km:.1f}km）"
        print(f"[渔船] 状态未变化：{status}，无需推送。")

    # 更新状态
    state.update({
        "in_port": is_in_port,
        "last_packet_time": ts,
        "last_lat": lat,
        "last_lon": lon,
    })
    save_state(state)
    print(f"[渔船] 状态已更新: in_port={is_in_port}")


if __name__ == "__main__":
    check_vessel()
