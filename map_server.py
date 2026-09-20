# -*- coding: utf-8 -*-
"""
地图 MCP Server（高德开放平台 Web 服务）
- maps_search(address)：地点搜索 / 地理编码（对应「地点查询」场景）
- maps_direction(origin, destination, mode)：路线规划（对应「路线查询」场景）
原生封装高德官方 API，避免依赖第三方不稳定 SSE 地址。
"""
import os
import sys
import re
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cacheutil import ttl_cache

load_dotenv()
mcp = FastMCP("MapServer")

AMAP_KEY = os.getenv("AMAP_KEY")
GEO_URL = "https://restapi.amap.com/v3/geocode/geo"
DIR_BASE = "https://restapi.amap.com/v3"
DIR_PATHS = {
    "driving": "direction/driving",
    "walking": "direction/walking",
    "bicycling": "direction/bicycling",
    "transit": "direction/transit/integrated",
}

# 常用中国城市集合：用于识别「北京」「广州」这类纯城市简称，地理编码时自动补全为「北京市」「广州市」，
# 避免高德把城市名解析成市内同名道路/商圈/门牌（如把「广州」解析成北京市内的广州路，导致北京→广州只有 5 公里）。
CHINESE_CITIES = {
    "北京", "上海", "广州", "深圳", "成都", "重庆", "杭州", "武汉", "西安", "苏州", "南京", "长沙",
    "天津", "郑州", "东莞", "青岛", "昆明", "宁波", "合肥", "佛山", "福州", "沈阳", "济南", "无锡",
    "厦门", "长春", "烟台", "廊坊", "南昌", "大连", "南宁", "温州", "石家庄", "哈尔滨", "泉州", "金华",
    "贵阳", "常州", "嘉兴", "珠海", "惠州", "南通", "中山", "太原", "徐州", "绍兴", "海口", "乌鲁木齐",
    "兰州", "扬州", "洛阳", "唐山", "呼和浩特", "镇江", "威海", "芜湖", "盐城", "潍坊", "三亚", "汕头",
    "赣州", "泰州", "济宁", "绵阳", "邯郸", "银川", "南阳", "淮安", "岳阳", "保定", "常德", "乌鲁木齐",
    "香港", "澳门", "台北",
}


def _city_fullname(addr: str) -> str:
    """对纯城市简称补全『市』后缀，避免地理编码歧义；复合地名保持原样。"""
    if not addr:
        return addr
    a = addr.strip()
    # 已经带市/县/区/站后缀，或长度明显不是纯城市名，不处理
    if a.endswith(("市", "县", "区", "镇", "乡", "站", "机场", "站")) or len(a) > 10:
        return a
    if a in CHINESE_CITIES:
        return a + "市"
    return a


# 国际大都市白名单：这些城市名统一指海外城市。高德地理编码可能误匹配到国内同名小地点
# （如「东京」被解析为广西东京镇、「洛杉矶」被解析为厦门楼盘），故路线规划一律按「需乘飞机」处理。
INTERNATIONAL_CITIES = {
    "东京", "大阪", "名古屋", "京都", "首尔", "釜山",
    "纽约", "洛杉矶", "旧金山", "芝加哥", "华盛顿",
    "伦敦", "曼彻斯特", "巴黎", "马赛", "柏林", "慕尼黑", "法兰克福",
    "罗马", "米兰", "马德里", "巴塞罗那",
    "曼谷", "新加坡", "吉隆坡", "悉尼", "墨尔本", "奥克兰",
    "莫斯科", "圣彼得堡", "多伦多", "温哥华",
}


@ttl_cache(604800, lambda addr, city=None: "geo:" + addr + "|" + str(city))  # 地点坐标 7 天不变
async def _geocode(addr: str, city: str | None = None) -> dict | None:
    params = {"address": addr, "key": AMAP_KEY}
    if city:
        params["city"] = city
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(GEO_URL, params=params)
        data = r.json()
    if data.get("status") != "1" or not data.get("geocodes"):
        return None
    gc = data["geocodes"][0]
    return {"name": gc.get("formatted_address"), "location": gc.get("location")}


@ttl_cache(604800, lambda addr, city=None: "poi:" + addr + ":" + str(city))  # POI 7 天不变
async def _poi_search(addr: str, city: str | None = None) -> dict | None:
    """POI 搜索（高德 inputtips）。对「陆家嘴」「人民广场」这类 POI 名比地理编码 geo 更准；
    city 限定可消除全国重名歧义（如『人民广场』）。作为 direction 定位失败/异常的兜底。"""
    params = {"keywords": addr, "key": AMAP_KEY}
    if city:
        params["city"] = city
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://restapi.amap.com/v3/assistant/inputtips", params=params)
        data = r.json()
    if data.get("status") != "1":
        return None
    for t in data.get("tips", []):
        loc = t.get("location")
        if loc:
            name = t.get("name") or addr
            district = t.get("district") or ""
            return {"name": f"{district}{name}".strip() or name, "location": loc}
    return None


def _city_of(geo: dict | None) -> str | None:
    """从高德地理编码结果的 name 中提取城市，如『上海市徐汇区…』-> 『上海』。"""
    if not geo or not geo.get("name"):
        return None
    name = geo["name"]
    # 优先：『四川省成都市…』-> 取 省 与 市 之间的城市名
    m = re.search(r"省([一-龥]{2,3})市", name)
    if m:
        return m.group(1)
    # 退化：直辖市如『北京市朝阳区…』-> 北京
    m = re.search(r"([一-龥]{2,3})市", name)
    if m:
        c = m.group(1)
        if not any(ch in c for ch in "省市区县镇乡"):
            return c
    return None


def _pick_convenient(transit_list):
    """从多条公共交通(transit)方案中选『方便前提下尽量快』的一条：
    排除含出租车接驳的方案，其余按 换乘次数少(方便门槛) > 时间短(尽量快) > 步行距离短(微调) 排序。"""
    def _has_taxi(t):
        # 仅当某段含非空 taxi 子对象才算含出租车（高德会给每段都塞空 taxi 键）
        return any(bool(s.get("taxi")) for s in t.get("segments", []))
    def _transfers(t):
        n = 0
        for s in t.get("segments", []):
            # railway/bus 子对象非空才算一段乘车
            if s.get("railway") or s.get("bus"):
                n += 1
        return max(0, n - 1)
    def _walk(t):
        try: return int(t.get("walking_distance", 0) or 0)
        except Exception: return 10 ** 9
    def _dur(t):
        try: return int(t.get("duration", 0) or 0)
        except Exception: return 10 ** 9
    pure = [t for t in transit_list if not _has_taxi(t)]
    pool = pure if pure else transit_list
    return min(pool, key=lambda t: (_transfers(t), _dur(t), _walk(t)))


@ttl_cache(1800, lambda o, d, mode, origin, destination: "dir:%s|%s|%s" % (o.get("location"), d.get("location"), mode))  # 路线 30min
async def _compute_direction(o: dict, d: dict, mode: str, origin: str, destination: str):
    """计算路线，返回 (dist_km, dur_min, text)。失败/异常时 text 为错误提示。"""
    path = DIR_PATHS.get(mode, "direction/driving")
    params = {"origin": o["location"], "destination": d["location"], "key": AMAP_KEY}
    if mode == "transit" and o.get("name"):
        params["city"] = _city_of(o) or o["name"].split("市")[0][:10]
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{DIR_BASE}/{path}", params=params)
        data = r.json()
    if data.get("status") != "1":
        return (None, None, f"❌ 路线规划失败：{data.get('info')}")
    if mode == "transit":
        transits = data.get("route", {}).get("transits", [{}])
        bus = _pick_convenient(transits)
        dur = int(bus.get("duration", 0)) // 60
        return (None, dur, f"🚌 {origin}→{destination}（公共交通）：约 {dur} 分钟")
    rs = data.get("route", {}).get("paths", [{}])[0]
    dist = int(rs.get("distance", 0)) / 1000.0
    dur = int(rs.get("duration", 0)) / 60.0
    steps = rs.get("steps", [])
    lines = [f"🚗 {origin}→{destination}（{mode}）：约 {dist:.1f} 公里，{dur:.0f} 分钟"]
    if steps:
        lines.append("\n📍 主要路段：")
        for i, s in enumerate(steps[:15], 1):
            detail = []
            if s.get("road"):
                detail.append(s["road"])
            if s.get("action"):
                detail.append(s["action"])
            if s.get("instruction"):
                detail.append(s["instruction"])
            if s.get("distance"):
                detail.append(f"{int(s['distance'])}米")
            if detail:
                lines.append(f"{i}. " + " | ".join(detail))
    return (dist, dur, "\n".join(lines))


@mcp.tool()
async def maps_search(address: str) -> str:
    """
    查询地点位置 / POI。
    :param address: 地点名称，如 "上海虹桥站"
    """
    if not AMAP_KEY:
        return "❌ 未配置 AMAP_KEY"
    res = await _geocode(address)
    if not res:
        return f"❓ 未找到地点：{address}"
    return f"📍 {res['name']}（坐标：{res['location']}）"


@mcp.tool()
async def maps_direction(origin: str, destination: str, mode: str = "driving") -> str:
    """
    规划两地之间的路线。
    :param origin: 起点，如 "北京南站"
    :param destination: 终点，如 "首都机场"
    :param mode: driving(驾车) / walking(步行) / bicycling(骑行) / transit(公共交通)
    """
    if not AMAP_KEY:
        return "❌ 未配置 AMAP_KEY"
    mode = mode or "driving"
    # 国际城市：驾车/高铁/公交路线规划均不适用，明确告知需乘飞机
    if origin in INTERNATIONAL_CITIES or destination in INTERNATIONAL_CITIES:
        city = destination if destination in INTERNATIONAL_CITIES else origin
        return (f"✈️ 「{city}」为海外城市，驾车/高铁/公交路线规划不适用，需要乘飞机前往。"
                f"当前 Demo 尚未接入航班查询工具，建议查询机票。")

    # 对纯城市简称补全为「北京市」「广州市」等，避免高德把「广州」解析成市内广州路
    origin_full = _city_fullname(origin)
    destination_full = _city_fullname(destination)

    # 判断起点/终点是否本身就是纯城市名；纯城市名地理编码时不应被对方的 city 限定
    # （否则「北京→广州」会被解析成北京城里的广州路）
    def _contains_city(name: str) -> bool:
        return any(c in name for c in CHINESE_CITIES)

    origin_is_city = origin.strip() in CHINESE_CITIES
    dest_is_city = destination.strip() in CHINESE_CITIES

    # 第一遍：地理编码（geo）。
    # - 纯城市名：直接查「北京市」「广州市」，不加 city 限定。
    # - 复合地名/POI：先尝试不加限定；若定位结果城市不对，再用对方城市兜底。
    o = await _geocode(origin_full)
    d = await _geocode(destination_full)
    o_city, d_city = _city_of(o), _city_of(d)

    # 纯 POI（起点/终点本身不含城市名）兜底：用对方城市限定重查，消除全国重名歧义。
    # 例如「人民广场」→ 终点城市限定；「北京→人民广场」→ 人民广场用北京限定。
    # 城市级查询（北京/广州）与含城市前缀的复合地名（上海虹桥站/广州白云机场）不做限定。
    if (not origin_is_city) and (not _contains_city(origin)):
        hint = d_city or (destination_full if dest_is_city else None)
        if hint:
            o = await _geocode(origin, hint)
            o_city = _city_of(o) or o_city
    if (not dest_is_city) and (not _contains_city(destination)):
        hint = o_city or (origin_full if origin_is_city else None)
        if hint:
            d = await _geocode(destination, hint)
            d_city = _city_of(d) or d_city
    # 无法定位 -> 各自用另一端城市做 hint，POI 搜索兜底重试
    if not o or not d:
        o_hint = d_city or (destination_full if dest_is_city else None)
        d_hint = o_city or (origin_full if origin_is_city else None)
        if not o:
            o = await _poi_search(origin, o_hint)
        if not d:
            d = await _poi_search(destination, d_hint)
        if not o or not d:
            return "❓ 起点或终点无法定位，请补充更明确的地点（建议带上城市，如『上海人民广场』）"

    dist, dur, text = await _compute_direction(o, d, mode, origin, destination)
    if text and (dist is None and dur is None):
        return text  # 失败提示

    # 异常拦截：地名歧义导致荒谬结果
    # A) 市内路线不可能 >500km / >10h
    LONG_DIST_KM, LONG_DUR_MIN = 500, 600
    # B) 预期跨城但结果过短（如「北京→广州」被解析成市内广州路，仅 5 公里）
    SHORT_CROSS_CITY_KM = 100
    o_city, d_city = _city_of(o), _city_of(d)
    cities_different = bool(o_city and d_city and o_city != d_city)
    looks_absurd = (
        (not cities_different and dist is not None and dist > LONG_DIST_KM) or
        (not cities_different and dur is not None and dur > LONG_DUR_MIN) or
        (cities_different and dist is not None and dist < SHORT_CROSS_CITY_KM)
    )
    if looks_absurd:
        hint = _city_of(d) or _city_of(o)
        # 重试 POI 搜索时也带上城市全称，进一步消除歧义
        o2 = await _poi_search(origin_full, hint)
        d2 = await _poi_search(destination_full, hint)
        if o2 and d2:
            dist2, dur2, text2 = await _compute_direction(o2, d2, mode, origin, destination)
            if text2 and (dist2 is None and dur2 is None):
                pass  # 重试也失败，保留原结果
            elif not ((dist2 or 0) > LONG_DIST_KM or (dur2 or 0) > LONG_DUR_MIN):
                text = text2  # 重试拿到合理结果，覆盖
    return text


if __name__ == "__main__":
    mcp.run(transport="stdio")
