#!/usr/bin/env python3
"""
粤珠渔养20003 靠港监控脚本 v3
MMSI: 412536814 | 目标港口: 洪湾渔港 (22.178°N, 113.437°E)

数据源策略:
  1. 船讯网 API（shipxy.com，中国近海最佳，需 SHIPXY_API_KEY）
  2. vesselapi.com REST API（实时，免费150次/月，按 MMSI 查询）
     [需配置 VESSELAPI_KEY，注册: https://dashboard.vesselapi.com]
  2. shipinfo.net API / 网页（降级，接受 ≤ SHIPINFO_MAX_AGE_HOURS 的数据）

防重推机制（方向A）:
  shipinfo 延迟高达14+小时，按数据源分别记录上次见到的 AIS 时间戳（last_ts_{source}），
  只有当前时间戳比该源上次记录更新才触发通知，避免同一条数据重复推送。
  不同数据源时间戳互相独立，不会互相干扰。

vesselapi 调用策略:
  仅在需要时调用（shipinfo 也失败时），节省免费额度（150次/月）。
  实际用量远低于上限，因为 shipinfo 在船停靠期间通常能提供够用的数据。
"""

import os, json, math, time, re, logging, threading
import requests
from datetime import datetime, timezone, timedelta

# ─── 配置 ─────────────────────────────────────────────────────────────────────
MMSI              = "412536814"
VESSEL_NAME       = "粤珠渔养20003"
PORT_LAT          = 22.178
PORT_LON          = 113.437
PORT_NAME         = "洪湾渔港"
ARRIVE_DIST_KM         = 1.5  # 距港 ≤1.5km 判定为靠港
ARRIVE_SPEED_KNT       = 1.0  # 船速 ≤1节 判定为靠港
SHIPINFO_MAX_AGE_HOURS = 20   # shipinfo降级数据最大接受年龄（方向A：放宽至20小时）
STATE_FILE        = "vessel_state.json"

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
_SHIPXY_KEY_RAW    = os.environ.get("SHIPXY_API_KEY", "")  # 船讯网免费试用 key

# 读取调度上下文
_schedule   = os.environ.get("GITHUB_SCHEDULE", "")
_event      = os.environ.get("GITHUB_EVENT", "")
_is_local   = (_event == "")  # 本地直接运行
_is_manual  = (_event == "workflow_dispatch")

# 船讯网：仅在 08:00/14:00 定时或手动触发时启用
_shipxy_schedules = {"0 0 * * 1-5", "0 6 * * 1-5"}
_use_shipxy = (
    _schedule in _shipxy_schedules
    or (_is_manual and os.environ.get("MANUAL_USE_SHIPXY", "true") != "false")
    or _is_local
)
SHIPXY_KEY = _SHIPXY_KEY_RAW if _use_shipxy else ""


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
    """
    检查 AIS 数据是否足够新鲜。
    - vesselapi 数据：实时REST API，直接接受（但仍做基本检查）
    - shipinfo 数据：延迟高，使用宽松阈值 SHIPINFO_MAX_AGE_HOURS（方向A）
    """
    ts = parse_ais_timestamp(pos.get("timestamp"))
    if ts is None:
        log.warning("数据缺少时间戳，无法验证新鲜度，丢弃")
        return False
    now = datetime.now(timezone.utc)
    age_h = (now - ts).total_seconds() / 3600

    # 根据数据源选择不同阈值
    source = pos.get("source", "")
    if source in ("vesselapi", "shipxy"):
        max_age = 2  # 实时REST API，超过2小时说明有问题
    else:
        max_age = SHIPINFO_MAX_AGE_HOURS  # shipinfo 允许最多20小时延迟

    if age_h > max_age:
        log.warning(
            "⚠️  AIS 数据过期！来源: %s，时间戳: %s UTC，已过去 %.1f 小时（阈值: %d 小时），丢弃",
            source, ts.strftime("%Y-%m-%d %H:%M"), age_h, max_age
        )
        return False
    log.info("✅ 数据新鲜度: %.0f 分钟前（来源: %s）", age_h * 60, source)
    return True


def is_newer_than_last(pos: dict, state: dict) -> bool:
    """
    方向A 防重推：shipinfo 延迟高，同一条数据可能跨多次检查都满足条件。
    按数据源分别记录上次见到的时间戳，只有当前时间戳比该源上次记录更新才允许触发。
    实时数据源（shipxy）直接放行。
    """
    source = pos.get("source", "")

    # 实时数据源直接放行
    if source == "shipxy":
        return True

    current_ts = parse_ais_timestamp(pos.get("timestamp"))
    if current_ts is None:
        return True  # 没有时间戳，保守允许

    # 按数据源分别存储时间戳，避免不同源之间互相干扰
    key = f"last_ts_{source}"
    last_ts_raw = state.get(key)
    if not last_ts_raw:
        return True  # 该源首次出现，允许

    last_ts = parse_ais_timestamp(last_ts_raw)
    if last_ts is None:
        return True

    if current_ts <= last_ts:
        log.info(
            "⏭️  [%s] 时间戳未推进（当前: %s，上次: %s），跳过",
            source,
            current_ts.strftime("%Y-%m-%d %H:%M"),
            last_ts.strftime("%Y-%m-%d %H:%M")
        )
        return False

    log.info("🆕 [%s] 时间戳推进: %s → %s",
             source,
             last_ts.strftime("%Y-%m-%d %H:%M"),
             current_ts.strftime("%Y-%m-%d %H:%M"))
    return True

# ─── 数据源 1: 船讯网 API（最优先，中国近海覆盖最佳）─────────────────────────
def fetch_shipxy() -> dict | None:
    """
    船讯网单船位置查询接口，专注中国近海，AIS 数据延迟最低。
    接口: GET https://api.shipxy.com/apicall/v3/GetSingleShip?key=KEY&mmsi=MMSI
    返回字段: lat, lng, sog, last_time（北京时间字符串）, last_time_utc（Unix时间戳）
    注册试用: https://api.shipxy.com（免费试用权限）
    需要环境变量 SHIPXY_API_KEY。
    """
    if not SHIPXY_KEY:
        log.info("[shipxy] 未配置 SHIPXY_API_KEY，跳过")
        return None

    url = "https://api.shipxy.com/apicall/v3/GetSingleShip"
    try:
        r = requests.get(url, params={"key": SHIPXY_KEY, "mmsi": MMSI}, timeout=15)
        log.info("[shipxy] HTTP %s", r.status_code)
        if r.status_code != 200:
            log.warning("[shipxy] HTTP 错误 %s", r.status_code)
            return None

        data = r.json()
        status = data.get("status", -1)

        # status 含义: 0=成功, 其他=错误（见文档附录「服务码返回说明」）
        if status != 0:
            log.warning("[shipxy] 返回错误码 status=%s msg=%s", status, data.get("msg", ""))
            return None

        d = data.get("data", {})
        lat = d.get("lat")
        lon = d.get("lng")
        spd = d.get("sog") or 0

        # last_time_utc 是 Unix 时间戳（秒），直接用；last_time 是北京时间字符串
        ts_unix = d.get("last_time_utc")
        if ts_unix:
            from datetime import timezone
            ts = datetime.fromtimestamp(int(ts_unix), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            ts = d.get("last_time")  # fallback: "2025-04-28 16:05:48" 北京时间（UTC+8）

        if lat is None or lon is None:
            log.warning("[shipxy] 响应中无坐标，完整响应: %s", json.dumps(data, ensure_ascii=False)[:500])
            return None

        log.info("[shipxy] lat=%.5f lon=%.5f spd=%.1f ts=%s", lat, lon, spd, ts)
        return {"lat": float(lat), "lon": float(lon), "speed": float(spd),
                "timestamp": ts, "source": "shipxy"}

    except Exception as e:
        log.warning("[shipxy] 异常: %s", e)
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
        log.debug("[shipinfo API] 完整响应: %s", json.dumps(data, ensure_ascii=False)[:1200])

        # 实际响应结构: data.latest.{lat, lng, speed_kn, updated}
        latest = data.get("data", {}).get("latest") or {}
        lat = latest.get("lat")
        lon = latest.get("lng") or latest.get("lon")   # shipinfo 用 lng 不是 lon
        spd = latest.get("speed_kn") or latest.get("speed") or 0
        ts  = latest.get("updated")                    # "2026-08-03 12:46:12" 格式

        if lat is None or lon is None:
            log.warning("[shipinfo API] data.latest 中未找到坐标，完整响应: %s",
                        json.dumps(data, ensure_ascii=False)[:1000])
            return None

        log.info("[shipinfo API] lat=%.5f lon=%.5f spd=%.1f ts=%s", lat, lon, spd, ts)
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
        m_s = re.search(r'SOG\s+([\d.]+)\s*kn', text, re.IGNORECASE)
        if m_s:
            spd = float(m_s.group(1))

        # 时间戳：优先匹配页面正文中的 "Latest AIS update: YYYY-MM-DD HH:MM:SS UTC"
        # 这是最可靠的字段，直接对应免费位置的时间
        ts = None
        m_ts = re.search(
            r'Latest AIS update[:\s]+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*UTC',
            text, re.IGNORECASE
        )
        if m_ts:
            ts = m_ts.group(1)  # "2026-08-03 11:43:14"
            log.info("[shipinfo web] 时间戳(Latest AIS update): %s", ts)
        else:
            # 降级：匹配页面内嵌 JSON 中与坐标同块的 updated 字段
            # 用坐标附近的文本窗口，避免抓到无关的 updated
            coord_str = f"{lat:.4f}"
            idx = text.find(coord_str)
            if idx > 0:
                window = text[max(0, idx-200):idx+500]
                m_ts2 = re.search(r'"updated"\s*:\s*"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"',
                                   window)
                if m_ts2:
                    ts = m_ts2.group(1)
                    log.info("[shipinfo web] 时间戳(JSON updated): %s", ts)

        if ts is None:
            log.warning("[shipinfo web] 未能提取时间戳，数据将被新鲜度检查丢弃")

        return {"lat": lat, "lon": lon, "speed": spd, "timestamp": ts, "source": "shipinfo_web"}
    except Exception as e:
        log.warning("[shipinfo web] 异常: %s", e)
        return None

# ─── 主逻辑 ───────────────────────────────────────────────────────────────────
SOURCES = [
    ("shipxy",       fetch_shipxy),       # 第一：船讯网，中国近海覆盖最佳
    ("shipinfo_api", fetch_shipinfo_api), # 第三：shipinfo API（允许20h延迟）
    ("shipinfo_web", fetch_shipinfo_web), # 第四：shipinfo 网页（兜底）
]

def get_vessel_position() -> dict | None:
    for name, fn in SOURCES:
        log.info("── 尝试数据源: %s ──", name)
        pos = fn()
        if pos is None:
            log.info("❌ [%s] 无数据", name)
            time.sleep(1)
            continue
        # 严格校验时间戳新鲜度（vesselapi 实时，shipinfo 用宽松阈值）
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
    log.info("shipinfo最大接受延迟: %dh", SHIPINFO_MAX_AGE_HOURS)
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
                f"（所有数据源失败或数据超过 {SHIPINFO_MAX_AGE_HOURS} 小时）\n"
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

    # 方向A 防重推：shipinfo 数据时间戳没有推进则跳过
    if not is_newer_than_last(pos, state):
        state["last_update"] = now_str
        state["status"] = now_s
        save_state(state)
        log.info("状态 %s，AIS时间戳未推进，静默", now_s)
        return

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
        if pos.get("timestamp"):
            state[f"last_ts_{pos['source']}"] = pos.get("timestamp")

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
        if pos.get("timestamp"):
            state[f"last_ts_{pos['source']}"] = pos.get("timestamp")

    else:
        log.info("状态无变化（%s），静默", now_s)
        state["status"] = now_s
        # 即使不推送也更新时间戳记录
        if pos.get("timestamp"):
            state[f"last_ts_{pos['source']}"] = pos.get("timestamp")

    state["last_update"] = now_str
    save_state(state)
    log.info("完成")

if __name__ == "__main__":
    main()
