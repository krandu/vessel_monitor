#!/usr/bin/env python3
"""
粤珠渔养20003 靠港监控脚本 v2
MMSI: 412536814 | 目标港口: 洪湾渔港 (22.178°N, 113.437°E)

数据源策略:
  1. aisstream.io WebSocket（实时，秒级延迟）[需配置 AISSTREAM_API_KEY]
  2. shipinfo.net（降级，仅接受 ≤2小时内的数据）
  关键保护: AIS 时间戳超过 MAX_DATA_AGE_HOURS 的数据一律丢弃
"""

import os, json, math, time, re, logging, asyncio
import requests
from datetime import datetime, timezone, timedelta

# ─── 配置 ─────────────────────────────────────────────────────────────────────
MMSI              = "412536814"
VESSEL_NAME       = "粤珠渔养20003"
PORT_LAT          = 22.178
PORT_LON          = 113.437
PORT_NAME         = "洪湾渔港"
ARRIVE_DIST_KM    = 1.5       # 距港 ≤1.5km
ARRIVE_SPEED_KNT  = 1.0       # 船速 ≤1节
MAX_DATA_AGE_HOURS = 2        # AIS数据超过此时长视为过期，拒绝使用
WEBSOCKET_TIMEOUT = 90        # aisstream.io 等待秒数（近海渔船AIS间隔可能较长）
STATE_FILE        = "vessel_state.json"

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
AISSTREAM_KEY     = os.environ.get("AISSTREAM_API_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ─── 工具函数 ──────────────────────────────────────────────────────────────────
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"status": "unknown", "last_notify": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram 未配置，跳过推送\n消息: %s", msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"
        }, timeout=15)
        r.raise_for_status()
        log.info("Telegram 推送成功")
    except Exception as e:
        log.error("Telegram 推送失败: %s", e)

def parse_ais_timestamp(ts_raw) -> datetime | None:
    """解析各种格式的 AIS 时间戳，统一返回 UTC aware datetime，失败返回 None"""
    if not ts_raw or ts_raw in ("unknown", "web"):
        return None
    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ]
    ts_str = str(ts_raw).rstrip("Z").split(".")[0]
    for fmt in formats:
        try:
            dt = datetime.strptime(ts_str[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # 尝试 unix timestamp
    try:
        return datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass
    log.warning("无法解析时间戳: %r", ts_raw)
    return None

def is_data_fresh(pos: dict) -> bool:
    """检查 AIS 数据是否足够新鲜（≤ MAX_DATA_AGE_HOURS 小时）"""
    ts = parse_ais_timestamp(pos.get("timestamp"))
    if ts is None:
        # 没有时间戳，无法判断新鲜度，保守拒绝
        log.warning("数据缺少时间戳，无法验证新鲜度，丢弃")
        return False
    now = datetime.now(timezone.utc)
    age = now - ts
    age_h = age.total_seconds() / 3600
    if age_h > MAX_DATA_AGE_HOURS:
        log.warning(
            "⚠️  AIS 数据过期！时间戳: %s UTC，已过去 %.1f 小时（阈值: %d 小时），丢弃",
            ts.strftime("%Y-%m-%d %H:%M"), age_h, MAX_DATA_AGE_HOURS
        )
        return False
    log.info("✅ 数据新鲜度: %.0f 分钟前", age.total_seconds() / 60)
    return True

# ─── 数据源 1: aisstream.io WebSocket（实时）─────────────────────────────────
def fetch_aisstream() -> dict | None:
    """
    连接 aisstream.io WebSocket，订阅指定 MMSI，等待第一条 PositionReport 消息。
    需要环境变量 AISSTREAM_API_KEY。
    注册地址: https://aisstream.io（免费，注册即得 API key）
    """
    if not AISSTREAM_KEY:
        log.info("[aisstream] 未配置 AISSTREAM_API_KEY，跳过")
        return None

    try:
        import websocket  # websocket-client
    except ImportError:
        log.warning("[aisstream] websocket-client 未安装，跳过")
        return None

    result = {}
    done = threading.Event()

    def on_open(ws):
        sub = {
            "APIKey": AISSTREAM_KEY,
            "MessageTypes": ["PositionReport"],
            "Filters": {"ShipMMSI": [MMSI]}
        }
        ws.send(json.dumps(sub))
        log.info("[aisstream] 已连接，等待 MMSI %s 的位置消息（最多 %ds）...", MMSI, WEBSOCKET_TIMEOUT)

    def on_message(ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get("MessageType", "")
            if msg_type == "PositionReport":
                pr = data["Message"]["PositionReport"]
                meta = data.get("MetaData", {})
                lat = pr.get("Latitude")
                lon = pr.get("Longitude")
                spd = pr.get("Sog", 0)          # Speed Over Ground
                ts  = meta.get("time_utc") or meta.get("TimeReceived")
                if lat is not None and lon is not None:
                    result.update({
                        "lat": float(lat), "lon": float(lon),
                        "speed": float(spd), "timestamp": ts,
                        "source": "aisstream"
                    })
                    log.info("[aisstream] 收到位置: lat=%.4f lon=%.4f spd=%.1f ts=%s",
                             lat, lon, spd, ts)
                    done.set()
                    ws.close()
        except Exception as e:
            log.warning("[aisstream] 消息解析错误: %s", e)

    def on_error(ws, error):
        log.warning("[aisstream] WebSocket 错误: %s", error)
        done.set()

    def on_close(ws, *args):
        done.set()

    import threading
    ws = websocket.WebSocketApp(
        "wss://stream.aisstream.io/v0/stream",
        on_open=on_open, on_message=on_message,
        on_error=on_error, on_close=on_close
    )
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()
    done.wait(timeout=WEBSOCKET_TIMEOUT)
    ws.close()

    if result:
        log.info("[aisstream] HTTP 成功")
        return result
    log.info("[aisstream] 超时（%ds内未收到该船位置报告）", WEBSOCKET_TIMEOUT)
    return None

# ─── 数据源 2: shipinfo.net API ───────────────────────────────────────────────
def fetch_shipinfo_api() -> dict | None:
    url = f"https://shipinfo.net/topos/api/vessel/summary?imo=0&mmsi={MMSI}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        log.info("[shipinfo API] HTTP %s", r.status_code)
        if r.status_code != 200:
            return None
        data = r.json()
        vessel = data.get("data") or data.get("vessel") or data
        lat = vessel.get("lat") or vessel.get("LATITUDE") or vessel.get("latitude")
        lon = vessel.get("lon") or vessel.get("LONGITUDE") or vessel.get("longitude")
        spd = vessel.get("speed") or vessel.get("SPEED") or vessel.get("sog") or 0
        ts  = vessel.get("timestamp") or vessel.get("TIMESTAMP") or vessel.get("time")
        if lat is None or lon is None:
            log.warning("[shipinfo API] 未找到坐标，完整响应: %s",
                        json.dumps(data, ensure_ascii=False)[:800])
            return None
        return {"lat": float(lat), "lon": float(lon), "speed": float(spd),
                "timestamp": ts, "source": "shipinfo_api"}
    except Exception as e:
        log.warning("[shipinfo API] 异常: %s", e)
        return None

# ─── 数据源 3: shipinfo.net 网页 ──────────────────────────────────────────────
def fetch_shipinfo_web() -> dict | None:
    url = f"https://shipinfo.net/vessels_map_mmsi_{MMSI}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        log.info("[shipinfo web] HTTP %s, len=%d", r.status_code, len(r.text))
        if r.status_code != 200:
            return None
        text = r.text
        patterns = [
            r'"lat"\s*:\s*([-\d.]+)\s*,\s*"lon"\s*:\s*([-\d.]+)',
            r'"latitude"\s*:\s*([-\d.]+)\s*,\s*"longitude"\s*:\s*([-\d.]+)',
            r'lat\s*=\s*([-\d.]+)[^,]*lon\s*=\s*([-\d.]+)',
        ]
        lat = lon = None
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
                break
        if lat is None:
            log.warning("[shipinfo web] 未找到坐标，片段:\n%s", text[:1500])
            return None
        spd = 0.0
        m_s = re.search(r'"speed"\s*:\s*([\d.]+)', text, re.IGNORECASE)
        if m_s:
            spd = float(m_s.group(1))
        # 提取时间戳
        ts = None
        for pat_ts in [r'"timestamp"\s*:\s*"([^"]+)"', r'"time"\s*:\s*"([^"]+)"',
                       r'"lastUpdate"\s*:\s*"([^"]+)"', r'"updated"\s*:\s*"([^"]+)"']:
            m_t = re.search(pat_ts, text, re.IGNORECASE)
            if m_t:
                ts = m_t.group(1)
                break
        return {"lat": lat, "lon": lon, "speed": spd, "timestamp": ts, "source": "shipinfo_web"}
    except Exception as e:
        log.warning("[shipinfo web] 异常: %s", e)
        return None

# ─── 主逻辑 ───────────────────────────────────────────────────────────────────
SOURCES = [
    ("aisstream",    fetch_aisstream),
    ("shipinfo_api", fetch_shipinfo_api),
    ("shipinfo_web", fetch_shipinfo_web),
]

def get_vessel_position() -> dict | None:
    for name, fn in SOURCES:
        log.info("── 尝试数据源: %s ──", name)
        pos = fn()
        if pos is None:
            log.info("❌ [%s] 无数据", name)
            time.sleep(1)
            continue
        # 严格校验时间戳新鲜度（aisstream 数据实时，也做检查）
        if not is_data_fresh(pos):
            log.warning("❌ [%s] 数据过期，丢弃，尝试下一个数据源", name)
            time.sleep(1)
            continue
        log.info("✅ [%s] lat=%.4f lon=%.4f spd=%.1f kn", name, pos["lat"], pos["lon"], pos["speed"])
        return pos
    return None

def format_age(ts_raw) -> str:
    ts = parse_ais_timestamp(ts_raw)
    if ts is None:
        return "时间未知"
    mins = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
    if mins < 60:
        return f"{mins} 分钟前"
    return f"{mins//60} 小时 {mins%60} 分钟前"

def main():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    log.info("=" * 60)
    log.info("开始监控 %s (MMSI: %s)", VESSEL_NAME, MMSI)
    log.info("运行时间: %s", now_str)
    log.info("AIS数据有效期阈值: %d 小时", MAX_DATA_AGE_HOURS)
    log.info("=" * 60)

    pos = get_vessel_position()

    if pos is None:
        log.warning("所有数据源均失败或数据过期，本次检查跳过（不改变状态，不推送）")
        # 只在连续多次失败时才推送告警（避免偶发失败骚扰）
        state = load_state()
        fail_count = state.get("consecutive_failures", 0) + 1
        state["consecutive_failures"] = fail_count
        save_state(state)
        if fail_count >= 6:  # 连续6小时失败才告警
            send_telegram(
                f"⚠️ <b>{VESSEL_NAME}</b> 数据异常\n"
                f"已连续 {fail_count} 小时无法获取有效位置数据\n"
                f"（所有数据源失败或数据超过 {MAX_DATA_AGE_HOURS} 小时）\n"
                f"🕐 {now_str}"
            )
        return

    dist_km = haversine_km(pos["lat"], pos["lon"], PORT_LAT, PORT_LON)
    # 若速度解析为0但位置不在港口附近，仅依靠距离判定（避免速度=0误判）
    speed_ok = pos["speed"] <= ARRIVE_SPEED_KNT
    dist_ok  = dist_km <= ARRIVE_DIST_KM
    in_port  = dist_ok and speed_ok

    log.info("📍 位置: %.4f°N, %.4f°E | ⚡ %.1f kn | 📏 距港 %.2f km | 靠港: %s",
             pos["lat"], pos["lon"], pos["speed"], dist_km, "是" if in_port else "否")

    state = load_state()
    state["consecutive_failures"] = 0   # 重置连续失败计数
    prev  = state.get("status", "unknown")
    now_s = "in_port" if in_port else "at_sea"

    if prev == "unknown":
        log.info("首次运行，记录初始状态: %s（不推送）", now_s)
        state.update({"status": now_s, "last_update": now_str})
        save_state(state)
        return

    ais_age = format_age(pos.get("timestamp"))

    if prev == "at_sea" and now_s == "in_port":
        msg = (
            f"⚓ <b>{VESSEL_NAME} 已返回{PORT_NAME}</b>\n\n"
            f"📍 位置: {pos['lat']:.4f}°N, {pos['lon']:.4f}°E\n"
            f"📏 距港心: {dist_km:.2f} km\n"
            f"⚡ 速度: {pos['speed']:.1f} 节\n"
            f"🕐 AIS数据: {ais_age}\n"
            f"📡 数据源: {pos['source']}\n"
            f"🔗 <a href='https://shipinfo.net/vessels_map_mmsi_{MMSI}'>查看位置</a>"
        )
        log.info("🚢 出海 → 靠港，推送通知")
        send_telegram(msg)
        state.update({"status": "in_port", "last_notify": now_str})

    elif prev == "in_port" and now_s == "at_sea":
        msg = (
            f"🌊 <b>{VESSEL_NAME} 已出海</b>\n\n"
            f"📍 位置: {pos['lat']:.4f}°N, {pos['lon']:.4f}°E\n"
            f"📏 距{PORT_NAME}: {dist_km:.2f} km\n"
            f"⚡ 速度: {pos['speed']:.1f} 节\n"
            f"🕐 AIS数据: {ais_age}\n"
            f"📡 数据源: {pos['source']}"
        )
        log.info("🌊 靠港 → 出海，推送通知")
        send_telegram(msg)
        state.update({"status": "at_sea", "last_notify": now_str})

    else:
        log.info("状态无变化（%s），静默", now_s)
        state["status"] = now_s

    state["last_update"] = now_str
    save_state(state)
    log.info("完成")

if __name__ == "__main__":
    main()
