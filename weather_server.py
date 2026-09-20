# -*- coding: utf-8 -*-
"""
天气 MCP Server（双源可切换）
- 默认 Open-Meteo：完全免费、无需 API Key、支持中文城市与 7 日预报（已实测可用）。
- 可选 OpenWeather：原 PDF 项目所用技术栈，免费版仅支持英文城市 + 5 天/3 小时预报，需 API Key。
- 通过环境变量 WEATHER_PROVIDER 切换（open_meteo / openweather），默认 open_meteo。
- query_weather(city, date=None, days=1)：支持「城市(中/英) + 日期 + 多日(days)」。
- get_weather_tips(season)：季节贴士（保留原项目能力）。
"""
import os
import sys
import httpx
from dotenv import load_dotenv
from datetime import date, datetime, timedelta
from mcp.server.fastmcp import FastMCP

# 保证与本服务同目录的 cacheutil 可被 import（MCP 子进程 cwd 可能与脚本目录不同）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cacheutil import ttl_cache

load_dotenv()
mcp = FastMCP("WeatherServer")

PROVIDER = os.getenv("WEATHER_PROVIDER", "open_meteo").lower()
OPENWEATHER_KEY = os.getenv("OPENWEATHER_API_KEY")
AMAP_KEY = os.getenv("AMAP_KEY")
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
AMAP_GEO_URL = "https://restapi.amap.com/v3/geocode/geo"
WX_URL = "https://api.open-meteo.com/v1/forecast"
OW_URL = "https://api.openweathermap.org/data/2.5/forecast"

# 中文城市 -> 英文（OpenWeather 免费版只认英文城市名）
CN2EN_CITY = {
    "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou", "深圳": "Shenzhen",
    "成都": "Chengdu", "杭州": "Hangzhou", "重庆": "Chongqing", "武汉": "Wuhan",
    "南京": "Nanjing", "西安": "Xi'an", "苏州": "Suzhou", "天津": "Tianjin",
    "长沙": "Changsha", "青岛": "Qingdao", "厦门": "Xiamen", "昆明": "Kunming",
    "大连": "Dalian", "哈尔滨": "Harbin", "郑州": "Zhengzhou", "济南": "Jinan",
    "宁波": "Ningbo", "合肥": "Hefei", "福州": "Fuzhou", "沈阳": "Shenyang",
}

WMO_CODES = {
    0: "晴", 1: "大部晴朗", 2: "多云", 3: "阴天",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨",
    56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "雨夹雪", 67: "雨夹雪",
    71: "小雪", 73: "中雪", 75: "大雪",
    77: "小雪",
    80: "阵雨", 81: "阵雨", 82: "暴雨",
    85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


def _wmo_desc(code: int) -> str:
    return WMO_CODES.get(code, "其他天气")


def _relative_label(target: date, d: date) -> str:
    """相对 target 的中文日期标签（今天/明天/后天/大后天），超出则显示 MM-DD。"""
    if d == target:
        return "今天"
    delta = (d - target).days
    labels = {1: "明天", 2: "后天", 3: "大后天"}
    if delta in labels:
        return labels[delta]
    return d.strftime("%m-%d")


def resolve_date(text: str | None) -> date:
    """把自然语言/日期字符串解析为 date 对象。"""
    if not text:
        return date.today()
    t = text.strip()
    today = date.today()
    table = {"今天": 0, "今日": 0, "today": 0,
             "明天": 1, "tomorrow": 1,
             "后天": 2, "day after tomorrow": 2,
             "大后天": 3}
    if t in table:
        return today + timedelta(days=table[t])
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    if t.startswith("下周"):
        weekday_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        if len(t) > 2:
            wd = weekday_map.get(t[-1])
            if wd is not None:
                days_ahead = (wd - today.weekday()) % 7 + 7
                return today + timedelta(days=days_ahead)
        # 纯「下周」无具体星期：兜底为下周一
        days_ahead = (0 - today.weekday()) % 7 + 7
        return today + timedelta(days=days_ahead)
    if "周末" in t:  # 「周末」「这周末」「下周末」→ 最近的周六
        days_ahead = (5 - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)
    return today


# ---------------- Open-Meteo ----------------
@ttl_cache(86400, lambda city: "amap_geo:" + city)  # 城市坐标 24h 不变
async def _geo_amap(city: str) -> dict | None:
    """用高德地理编码解析中文城市/区县，更准确。

    返回 dict 包含 extra 元数据，用于后续判断结果是否可信。
    """
    if not AMAP_KEY:
        return None
    params = {"address": city, "key": AMAP_KEY}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(AMAP_GEO_URL, params=params)
        data = r.json()
    if data.get("status") != "1":
        return None
    gcs = data.get("geocodes", [])
    if not gcs:
        return None
    gc = gcs[0]
    loc = gc.get("location", "")
    if not loc or "," not in loc:
        return None
    lon, lat = loc.split(",")
    return {
        "name": gc.get("formatted_address", city),
        "lat": float(lat),
        "lon": float(lon),
        "country": gc.get("country", ""),
        "level": gc.get("level", ""),
        "formatted_address": gc.get("formatted_address", ""),
        "province": gc.get("province", ""),
        "city": gc.get("city", ""),
    }


def _is_chinese_text(text: str) -> bool:
    """判断字符串是否主要为中文（含至少一个汉字）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


OVERSEAS_PREFIXES = (
    "美国", "英国", "法国", "德国", "日本", "韩国", "澳大利亚", "加拿大",
    "俄罗斯", "意大利", "西班牙", "泰国", "新加坡", "马来西亚", "新西兰",
)


def _amap_is_reliable(loc: dict, city: str) -> bool:
    """判断高德地理编码结果是否可信。

    主要处理两类误匹配：
    1. 国外城市被匹配到国内同名楼盘/兴趣点（如洛杉矶→厦门源昌国际城）
    2. 返回地址级别过低（具体门牌/楼栋）且不含原始城市名
    """
    # 高德主要覆盖中文地址；英文城市名直接用 Open-Meteo 更准
    if not _is_chinese_text(city):
        return False
    # 明确带国外国家/地区前缀的，跳过高德
    if any(city.startswith(p) or city.endswith(p) for p in OVERSEAS_PREFIXES):
        return False
    # 常见国外城市中文名（如东京、伦敦、巴黎），优先用 Open-Meteo 避免国内同名小地点误匹配
    if city in OVERSEAS_CITY_EN:
        return False
    country = loc.get("country", "")
    level = loc.get("level", "")
    addr = loc.get("formatted_address", "")
    # 非中国结果不可信
    if country and country != "中国":
        return False
    # 级别过低且出现楼栋/门牌号特征，大概率是楼盘误匹配
    low_levels = ("兴趣点", "门牌号", "道路", "路口", "街道")
    if level in low_levels:
        if any(k in addr for k in ("号楼", "栋", "单元", "室", "广场", "中心", "大厦")):
            return False
        if city not in addr:
            return False
    return True


COUNTRY_PREFIX_MAP = {
    "美国": "United States", "英国": "United Kingdom", "法国": "France",
    "德国": "Germany", "日本": "Japan", "韩国": "South Korea",
    "澳大利亚": "Australia", "加拿大": "Canada", "俄罗斯": "Russia",
    "意大利": "Italy", "西班牙": "Spain", "泰国": "Thailand",
    "新加坡": "Singapore", "马来西亚": "Malaysia", "新西兰": "New Zealand",
}

# 常见国外城市中文 -> 英文，用于 Open-Meteo 地理编码
OVERSEAS_CITY_EN = {
    "洛杉矶": "Los Angeles", "纽约": "New York", "旧金山": "San Francisco",
    "华盛顿": "Washington", "芝加哥": "Chicago", "波士顿": "Boston",
    "伦敦": "London", "曼彻斯特": "Manchester", "伯明翰": "Birmingham",
    "巴黎": "Paris", "马赛": "Marseille", "里昂": "Lyon",
    "柏林": "Berlin", "慕尼黑": "Munich", "法兰克福": "Frankfurt",
    "东京": "Tokyo", "大阪": "Osaka", "京都": "Kyoto", "名古屋": "Nagoya",
    "首尔": "Seoul", "釜山": "Busan",
    "悉尼": "Sydney", "墨尔本": "Melbourne", "布里斯班": "Brisbane",
    "多伦多": "Toronto", "温哥华": "Vancouver",
    "莫斯科": "Moscow", "圣彼得堡": "Saint Petersburg",
    "罗马": "Rome", "米兰": "Milan",
    "马德里": "Madrid", "巴塞罗那": "Barcelona",
    "曼谷": "Bangkok", "清迈": "Chiang Mai",
    "新加坡": "Singapore", "吉隆坡": "Kuala Lumpur",
    "奥克兰": "Auckland", "惠灵顿": "Wellington",
}


@ttl_cache(86400, lambda city: "meteo_geo:" + city)  # 城市坐标 24h 不变
async def _geo_meteo(city: str) -> dict | None:
    """Open-Meteo 自带 geocoding，作为高德失败后的 fallback。"""
    async def _search(name: str, lang: str = "zh") -> list[dict]:
        params = {"name": name, "count": 5, "language": lang, "format": "json"}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(GEO_URL, params=params)
            data = r.json()
        return data.get("results") or []

    def _pick(results: list[dict], preferred_country: str | None = None) -> dict | None:
        if not results:
            return None
        if preferred_country:
            for loc in results:
                if loc.get("country") == preferred_country:
                    return loc
        return results[0]

    def _as_loc(loc: dict, name: str) -> dict:
        return {
            "name": loc.get("name", name),
            "lat": loc["latitude"],
            "lon": loc["longitude"],
            "country": loc.get("country", ""),
        }

    # 常见国外城市中文名，直接用英文搜索更准（避免中文返回国内同名小地方）
    if city in OVERSEAS_CITY_EN:
        results = await _search(OVERSEAS_CITY_EN[city], lang="en")
        if results:
            return _as_loc(results[0], OVERSEAS_CITY_EN[city])

    preferred_country = None
    matched_prefix = ""
    for prefix, country in COUNTRY_PREFIX_MAP.items():
        if city.startswith(prefix):
            preferred_country = country
            matched_prefix = prefix
            break

    # 1) 若含国家/地区前缀（如"美国洛杉矶"），优先用英文搜索去掉前缀后的城市名，并按国家过滤
    if preferred_country:
        short = city[len(matched_prefix):].strip()
        # 国外大城市用英文搜索更准确；常见城市做中文->英文映射
        en_name = OVERSEAS_CITY_EN.get(short)
        results = await _search(en_name or short, lang="en")
        loc = _pick(results, preferred_country)
        if loc:
            return _as_loc(loc, en_name or short)

    # 2) 按原城市名搜索
    results = await _search(city)
    loc = _pick(results, preferred_country)
    if loc:
        return _as_loc(loc, city)

    # 3) 若含国家前缀但中文搜索未果，去掉前缀再试中文
    if preferred_country:
        short = city[len(matched_prefix):].strip()
        results = await _search(short)
        loc = _pick(results, preferred_country)
        if loc:
            return _as_loc(loc, short)

    # 4) 对纯中文城市再试英文
    if _is_chinese_text(city):
        results = await _search(city, lang="en")
        loc = _pick(results)
        if loc:
            return _as_loc(loc, city)
    return None


@ttl_cache(900, lambda lat, lon, days: "wx:%.3f,%.3f,%d" % (lat, lon, days))  # 天气 15min
async def _wx_meteo(lat: float, lon: float, days: int = 7) -> dict:
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_mean,weather_code",
        "timezone": "auto", "forecast_days": days,
    }
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(WX_URL, params=params)
        return r.json()


async def _query_meteo(city: str, date: str | None = None, days: int = 1) -> str:
    # 优先用高德地理编码（中文更准），但当高德误匹配到国外城市/楼盘时回退 Open-Meteo
    loc = await _geo_amap(city)
    if loc and not _amap_is_reliable(loc, city):
        loc = None
    if not loc:
        loc = await _geo_meteo(city)
    if not loc:
        return f"❓ 未找到城市：{city}，请尝试使用更常见的城市名（如'内江'）"
    days = max(1, min(int(days), 16))
    data = await _wx_meteo(loc["lat"], loc["lon"], days=16)
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    if not dates:
        return "❌ 天气数据为空"
    target = resolve_date(date)
    target_str = target.strftime("%Y-%m-%d")
    if target_str not in dates:
        return (f"❌ 无法查询 {city} {target_str} 的天气。"
                f"当前仅支持 {dates[0]} 至 {dates[-1]} 的预报，请调整日期。")
    start_idx = dates.index(target_str)
    end_idx = min(start_idx + days, len(dates))
    lines = []
    for i in range(start_idx, end_idx):
        d = datetime.strptime(dates[i], "%Y-%m-%d").date()
        desc = _wmo_desc(daily["weather_code"][i])
        tmax = daily["temperature_2m_max"][i]
        tmin = daily["temperature_2m_min"][i]
        precip = daily["precipitation_probability_mean"][i]
        label = _relative_label(target, d)
        lines.append(f"- {dates[i]}（{label}）：{desc}，气温 {tmin}~{tmax}°C，降水概率 {precip}%")
    if days == 1:
        # 单日格式保持与旧版一致，兼容既有测试集
        return (f"{loc['name']}（{loc['lat']:.3f}°N, {loc['lon']:.3f}°E）"
                f" {dates[start_idx]} 天气：{lines[0].split('：', 1)[1]}")
    header = (f"{loc['name']}（{loc['lat']:.3f}°N, {loc['lon']:.3f}°E）"
              f" 天气预报（{dates[start_idx]} 至 {dates[end_idx-1]}）：")
    return header + "\n" + "\n".join(lines)


# ---------------- OpenWeather（原 PDF 技术栈）----------------
def _to_english(city: str) -> str:
    """OpenWeather 免费版只接受英文城市名。"""
    return CN2EN_CITY.get(city, city)


async def _query_openweather(city: str, date: str | None = None, days: int = 1) -> str:
    if not OPENWEATHER_KEY:
        return "❌ 未配置 OPENWEATHER_API_KEY"
    en = _to_english(city)
    params = {"q": en, "appid": OPENWEATHER_KEY, "units": "metric", "lang": "zh_cn"}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(OW_URL, params=params)
        data = r.json()
    if data.get("cod") != "200":
        return f"❌ OpenWeather 查询失败：{data.get('message', '未知错误')}（城市：{city}）"
    # 5 天 / 3 小时预报，按日期分组后聚合
    from collections import defaultdict
    by_day = defaultdict(list)
    for it in data["list"]:
        by_day[it["dt_txt"][:10]].append(it)
    day_keys = sorted(by_day.keys())
    if not day_keys:
        return "❌ 天气数据为空"
    target = resolve_date(date)
    target_str = target.strftime("%Y-%m-%d")
    if target_str in by_day:
        start = day_keys.index(target_str)
    else:
        start = 0
    days = max(1, min(int(days), len(day_keys) - start))
    end = start + days
    lines = []
    for d in day_keys[start:end]:
        items = by_day[d]
        temps = [it["main"]["temp"] for it in items]
        descs = [it["weather"][0]["description"] for it in items]
        pops = [it.get("pop", 0) for it in items]
        tmax, tmin = max(temps), min(temps)
        desc = max(set(descs), key=descs.count)
        precip = int(max(pops) * 100)
        label = _relative_label(target, datetime.strptime(d, "%Y-%m-%d").date())
        lines.append(f"- {d}（{label}）：{desc}，气温 {tmin:.0f}~{tmax:.0f}°C，降水概率 {precip}%")
    if days == 1:
        return f"{city} {target_str} 天气：{lines[0].split('：', 1)[1]}"
    return f"{city} 天气预报（{day_keys[start]} 至 {day_keys[end-1]}）：\n" + "\n".join(lines)


@mcp.tool()
async def query_weather(city: str, date: str = None, days: int = 1) -> str:
    """
    查询指定城市的天气，支持日期与多日预报。
    :param city: 城市名称（Open-Meteo 源支持中文；OpenWeather 源支持英文，中文会做映射）
    :param date: 可选，"今天/明天/后天" 或 "YYYY-MM-DD"，缺省为今天；作为多日预报的起始日
    :param days: 可选，返回从 date 起连续 days 天的预报（1~16），缺省为 1（单日）
    :return: 格式化后的天气信息
    """
    if PROVIDER == "openweather":
        return await _query_openweather(city, date, days)
    return await _query_meteo(city, date, days)


@mcp.tool()
async def get_weather_tips(season: str) -> str:
    """
    获取指定季节的天气贴士。
    :param season: 季节名称 (spring, summer, autumn, winter)
    """
    tips = {
        "spring": "🌸 春季多风，注意防风保暖",
        "summer": "☀️ 夏季炎热，注意防暑",
        "autumn": "🍂 秋季干燥，注意补水",
        "winter": "❄️ 冬季寒冷，注意防寒",
    }
    return tips.get(season.lower(), "❓ 未知季节")


if __name__ == "__main__":
    mcp.run(transport="stdio")
