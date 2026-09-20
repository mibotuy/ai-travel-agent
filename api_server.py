# -*- coding: utf-8 -*-
"""FastAPI 服务：暴露 /chat 接口（含工具调用步骤），并提供前端可视化界面。

启动： source .venv/bin/activate && python api_server.py
访问： http://localhost:8000
"""
import os
import uuid
import asyncio
import sqlite3
import threading
import time
import re
import glob
import json as _json
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from agent import run_query, run_query_stream
from weather_server import CN2EN_CITY, OVERSEAS_CITY_EN

app = FastAPI(title="AI 智能出行助手 Agent")

# 内存会话历史：session_id -> [(role, content), ...]
SESSIONS = {}

# 用于识别省略天气问法的城市名集合
EXTRA_CITIES = {
    "东莞", "苏州", "无锡", "常州", "佛山", "珠海", "惠州", "中山", "江门", "汕头",
    "湛江", "肇庆", "茂名", "阳江", "清远", "揭阳", "梅州", "韶关", "河源", "汕尾",
    "云浮", "潮州", "香港", "澳门", "台北", "高雄", "台中", "台南",
    "威远县", "威远", "内江", "自贡", "泸州", "宜宾", "南充", "达州", "遂宁", "广安",
    "巴中", "眉山", "乐山", "雅安", "资阳", "阿坝", "甘孜", "凉山", "攀枝花", "绵阳",
    "德阳", "广元", "遂宁市", "资阳市", "北京市", "上海市", "广州市", "深圳市",
}
KNOWN_CITIES = set(CN2EN_CITY.keys()) | set(OVERSEAS_CITY_EN.keys()) | EXTRA_CITIES
ROUTE_KEYWORDS = ("从", "到", "去", "怎么走", "路线", "导航", "地图", "途经", "距离", "公里")
ROUTE_ACTION_KEYWORDS = ("怎么走", "怎么去", "路线", "导航", "高铁", "火车", "列车", "车次", "规划", "出行方案", "怎么到")
WEATHER_ELLIPSIS = ("呢", "天气", "怎么样", "如何", "好吗", "最近", "未来", "降雨", "下雨",
                    "下雪", "气温", "温度", "冷不冷", "热不热", "适合出游", "适合旅行",
                    "这几天", "最近几天", "未来几天")

# 明确的单日/多日标记
SINGLE_DAY_MARKERS = ("今天", "今日", "明天", "后天", "大后天")
MULTI_DAY_MARKERS = ("最近", "未来", "几天", "这几天", "最近几天", "未来几天", "最近天气")


def expand_weather_question(question: str, history: list[tuple[str, str]] | None = None) -> str:
    """把'东京呢''北京天气'这类省略问法补全为完整天气查询。

    避免 ReAct Agent 在多轮上下文中把上一轮其他城市的数据套用到新城市。
    但遇到'今天/明天/后天'等明确单日标记时，保留单日意图，不要改写成'最近天气'。
    对于'呢'这类泛语气词，只在当前会话最近也是天气话题时才补全，防止
    '上海迪士尼呢'在地图/路线语境下被误判成天气查询。
    """
    q = question.strip()
    # 路线意图不处理
    if any(k in q for k in ROUTE_KEYWORDS):
        return q
    # 较长句子通常意图明确，不强制扩展；但如果是极简短省略问法仍补全
    if len(q) > 18:
        return q
    # 判断当前会话是否以天气为主：最近用户消息含天气关键词
    weather_context = False
    if history:
        recent_user_texts = [content for role, content in history[-6:] if role == "user"]
        weather_context = any(
            any(k in t for k in ("天气", "气温", "温度", "降水", "降雨", "下雪", "带伞", "冷不冷", "热不热"))
            for t in recent_user_texts
        )
    # 按长度降序匹配，避免'北京'匹配到'北海道'
    for city in sorted(KNOWN_CITIES, key=len, reverse=True):
        if city in q:
            # 用户明确说了今天/明天/后天/大后天：保留单日意图。
            # 极简短省略问法才补全；较长句子保留原句，避免截断"带伞吗"等附加信息。
            for marker in SINGLE_DAY_MARKERS:
                if marker in q:
                    if len(q) <= 10:
                        return f"{city}{marker}天气怎么样"
                    return q
            # 用户明确说多日/未来/最近：补全为最近天气
            if any(k in q for k in MULTI_DAY_MARKERS):
                return f"{city}最近天气怎么样"
            # 其他天气相关省略问法，默认用'最近天气'强制触发工具
            if any(k in q for k in WEATHER_ELLIPSIS):
                # "呢"单独出现时比较泛，只在天气语境里才补全；
                # 若同时含其他天气词（如'天气''怎么样'），仍正常补全。
                if "呢" in q and not any(k in q for k in WEATHER_ELLIPSIS if k != "呢"):
                    if not weather_context:
                        break
                return f"{city}最近天气怎么样"
            break
    return q


def expand_route_question(question: str) -> str:
    """把'深圳到北京呢'这类省略路线问法补全为完整查询。

    防止 ReAct Agent 在多轮上下文中把上一轮其他城市的路线方案套用到新城市，
    或误以为上下文已涵盖而省略 query_train 调用（与 expand_weather_question 同理）。
    """
    q = question.strip()
    # 已含明确路线动作词，意图清晰，不处理
    if any(k in q for k in ROUTE_ACTION_KEYWORDS):
        return q
    # 非路线意图（不含 从/到/去 等）不处理
    if not any(k in q for k in ROUTE_KEYWORDS):
        return q
    # 含"帮/请/查/看"等动词前缀的复合句意图已清晰，不强行补全
    if any(p in q for p in ("帮", "请", "查", "看", "问", "我想", "可以", "能否", "麻烦", "给我")):
        return q
    # 路线省略问法：含语气词"呢"或以问号结尾，或整体很短
    is_ellipsis = ("呢" in q or "？" in q or "?" in q or len(q) <= 14)
    if not is_ellipsis:
        return q
    # 补全为"从 X 到 Y 怎么走"，强制触发 query_train
    filled = q.replace("呢", "").replace("？", "").replace("?", "").strip()
    if not filled.startswith("从"):
        filled = "从" + filled
    filled = filled + "怎么走"
    return filled


# 省份 → 省会，用于"去X省呢"这类问法（省份不在城市表里，需映射为省会才能查天气/路线）
PROVINCE_CAPITAL = {
    "四川": "成都", "湖南": "长沙", "湖北": "武汉", "福建": "福州", "广东": "广州",
    "浙江": "杭州", "江苏": "南京", "山东": "济南", "河南": "郑州", "河北": "石家庄",
    "辽宁": "沈阳", "吉林": "长春", "黑龙江": "哈尔滨", "陕西": "西安", "山西": "太原",
    "安徽": "合肥", "江西": "南昌", "云南": "昆明", "贵州": "贵阳", "甘肃": "兰州",
    "青海": "西宁", "海南": "海口", "台湾": "台北", "广西": "南宁", "内蒙古": "呼和浩特",
    "宁夏": "银川", "新疆": "乌鲁木齐", "西藏": "拉萨",
}

# 出差省略问法跨轮保日期：相对/绝对日期标记
_DATE_MARKERS = ("大前天", "前天", "大后天", "后天", "明天", "明日", "今天", "今日",
                 "下周", "周末")
_ABS_DATE_RE = re.compile(r"\d{1,2}月\d{1,2}[日号]|\d{4}[-/]\d{1,2}[-/]\d{1,2}")


def _extract_date_marker(text: str) -> str | None:
    """从问句中抽取相对/绝对日期标记；找不到返回 None。"""
    for m in _DATE_MARKERS:
        if m in text:
            return m
    am = _ABS_DATE_RE.search(text)
    return am.group(0) if am else None


def expand_biz_followup(question: str, history: list[tuple[str, str]] | None = None) -> str:
    """把"出差语境下的省略目的地问法"确定性地还原为完整出差需求。

    当历史中用户刚问过"从A去B出差，看天气和路线"，后续再说"去X呢""X呢""去X"
    等短问句时，直接在代码层把问题改写成"从A去X出差，看天气和路线"，由下方的
    天气/路线扩展逻辑确定性地触发三个工具。这样不依赖模型"记性"，对任意城市/省份都生效，
    也避免 Prompt 偶发漏掉天气查询。
    """
    q = question.strip()
    # 仅针对短小的省略目的地问法
    if len(q) > 18:
        return q
    if not history:
        return q
    # 取最近一条用户消息，判断是否处于"出差 + 天气/路线"语境
    last_user = ""
    for role, content in reversed(history[-6:]):
        if role == "user":
            last_user = content
            break
    if not (("出差" in last_user) and ("天气" in last_user or "路线" in last_user)):
        return q
    # 从当前问题中抽取目的地：先匹配已知城市，再匹配省份（映射为省会）
    dest = None
    for city in sorted(KNOWN_CITIES, key=len, reverse=True):
        if city in q:
            dest = city
            break
    if not dest:
        for prov, cap in PROVINCE_CAPITAL.items():
            if prov in q:
                dest = cap
                break
    if not dest:
        return q
    # 从上一句抽取出发地（"从X去/到Y"）
    origin = None
    m = re.search(r"从(.+?)去", last_user) or re.search(r"从(.+?)到", last_user)
    if m:
        origin = m.group(1).strip()
    # 跨轮保日期：若本轮省略问法未自带日期，则沿用上一轮出差句的日期（如"后天"），
    # 避免"后天从北京去广州 → 去上海呢"时第二轮悄悄回落到"今天"，造成日期前后不一致。
    carried = ""
    if not _extract_date_marker(q) and "出差" in last_user:
        prev_date = _extract_date_marker(last_user)
        if prev_date:
            carried = f"，日期与上一轮一致为{prev_date}"
    if origin:
        return (f"从{origin}去{dest}出差{carried}，请先查询{dest}的天气，"
                f"并规划从{origin}去{dest}的高铁与驾车路线")
    return f"{dest}出差{carried}，请先查询{dest}的天气，并规划高铁与驾车路线"


def _to_history(session_id: str | None):
    """将会话历史转换为 agent.run_query 需要的 (role, content) 列表。
    内存 SESSIONS 优先；若进程重启丢失，则从持久化的对话记录恢复，保证多轮上下文不丢。"""
    if not session_id:
        return []
    if session_id in SESSIONS:
        return SESSIONS[session_id]
    msgs = _load_messages(session_id)
    if msgs:
        hist = []
        for m in msgs:
            # 兼容两种历史存储格式：
            #  - 当前格式：dict {"role","text"}
            #  - 早期版本格式：list/tuple ["role","text"]（元组经 JSON 序列化后变成数组）
            if isinstance(m, dict):
                role, text = m.get("role"), (m.get("text") or "")
            elif isinstance(m, (list, tuple)) and len(m) >= 2:
                role, text = m[0], m[1]
            else:
                continue
            if role in ("user", "bot", "assistant"):
                hist.append((role if role != "bot" else "assistant", text))
        SESSIONS[session_id] = hist
        return hist
    return []


def _append_turn(session_id: str, question: str, answer: str):
    if session_id not in SESSIONS:
        SESSIONS[session_id] = []
    SESSIONS[session_id].append(("user", question))
    SESSIONS[session_id].append(("assistant", answer))
    # 保留最近 10 轮（20 条消息），降低长会话时的 LLM 延迟与网关超时风险
    SESSIONS[session_id] = SESSIONS[session_id][-20:]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- 对话历史持久化（侧边栏 / 跨刷新保留） ----------
# 按 client_id（浏览器身份）隔离，每段对话为一条记录，messages 存完整消息（含工具步骤）。
DB_PATH = os.path.join(BASE_DIR, "conversations.db")
_DB_LOCK = threading.Lock()


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            title TEXT,
            messages TEXT,
            created_at REAL,
            updated_at REAL
        )"""
    )
    return conn


def _load_messages(cid):
    try:
        with _DB_LOCK:
            conn = _db()
            row = conn.execute("SELECT messages FROM conversations WHERE id=?", (cid,)).fetchone()
            conn.close()
        if not row or not row["messages"]:
            return []
        return _json.loads(row["messages"])
    except Exception:
        return []


def _save_messages(cid, client_id, messages, title=None):
    """写入整段对话；标题为空或以默认占位时，用首条用户消息自动命名。"""
    try:
        now = time.time()
        with _DB_LOCK:
            conn = _db()
            row = conn.execute("SELECT title, created_at FROM conversations WHERE id=?", (cid,)).fetchone()
            if row:
                cur = title if title is not None else (row["title"] or "")
                if not cur or cur == "新对话":
                    for m in messages:
                        if m.get("role") == "user":
                            cur = (m.get("text") or "")[:30]
                            break
                created = row["created_at"] or now
                conn.execute(
                    "UPDATE conversations SET messages=?, title=?, updated_at=? WHERE id=?",
                    (_json.dumps(messages, ensure_ascii=False), cur, now, cid),
                )
            else:
                t = title or ""
                if not t or t == "新对话":
                    for m in messages:
                        if m.get("role") == "user":
                            t = (m.get("text") or "")[:30]
                            break
                conn.execute(
                    "INSERT INTO conversations (id, client_id, title, messages, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (cid, client_id or "", t, _json.dumps(messages, ensure_ascii=False), now, now),
                )
            conn.commit()
            conn.close()
    except Exception:
        pass


def _derive_notes(names):
    notes = []
    if "query_weather" in names:
        notes.append("🌤️ 天气为 Open-Meteo / 高德 预报参考")
    if "query_train" in names:
        notes.append("🚄 高铁为线路参考，实时车次/票价/余票以 12306 为准")
    if any(n.startswith("maps_") for n in names):
        notes.append("🗺️ 路线来自高德地图，具体以导航 App 为准")
    return notes


class ChatRequest(BaseModel):
    question: str
    session_id: str | None = None
    client_id: str | None = None


@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        session_id = req.session_id or str(uuid.uuid4())
        history = _to_history(session_id)
        # 出差语境下的省略目的地问法（如"去X呢"）确定性还原为完整需求
        q_pre = expand_biz_followup(req.question, history=history)
        # 对省略天气问法做补全，防止模型在多轮中复用其他城市数据
        q0 = expand_weather_question(q_pre, history=history)
        q1 = expand_route_question(q0)
        question = q1
        # 多轮下若出现路线省略补全（如「深圳到北京呢」→「从深圳到北京怎么走」），
        # 强制要求调用 query_train，避免 LLM 依赖历史上下文而跳过工具调用
        if q1 != q0 and history:
            question = q1 + ("（这是一次新的独立路线查询，请务必调用 query_train 工具获取方案，"
                             "不要复用或依赖上方历史对话内容，也不要仅凭记忆回答）")
        final, tool_calls, steps = await run_query(question, history=history)
        _append_turn(session_id, req.question, final)
        return {
            "session_id": session_id,
            "answer": final,
            "tool_calls": [{"name": t["name"], "args": t["args"]} for t in tool_calls],
            "steps": steps,
        }
    except Exception as e:  # noqa
        return {
            "session_id": req.session_id,
            "answer": f"⚠️ 调用出错：{e}",
            "tool_calls": [],
            "steps": [],
        }


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """SSE 流式端点：逐步推送工具步骤与答案 token，提升感知延迟。
    正确性逻辑与 /chat 完全一致（复用同样的 expand_* 多轮补全与 run_query_stream）。
    每轮结束后把完整对话（含工具步骤）落库，供侧边栏历史列表展示与跨刷新恢复。"""
    session_id = req.session_id or str(uuid.uuid4())
    client_id = req.client_id
    history = _to_history(session_id)
    q_pre = expand_biz_followup(req.question, history=history)
    q0 = expand_weather_question(q_pre, history=history)
    q1 = expand_route_question(q0)
    question = q1
    if q1 != q0 and history:
        question = q1 + ("（这是一次新的独立路线查询，请务必调用 query_train 工具获取方案，"
                         "不要复用或依赖上方历史对话内容，也不要仅凭记忆回答）")

    async def event_gen():
        full_answer = []
        steps = []
        tool_names = []
        try:
            yield "event: meta\ndata: " + _json.dumps(
                {"session_id": session_id}, ensure_ascii=False) + "\n\n"
            async for item in run_query_stream(question, history=history):
                if item["type"] == "tool_start":
                    yield "event: tool_start\ndata: " + _json.dumps(
                        {"name": item["name"], "args": item["args"]},
                        ensure_ascii=False) + "\n\n"
                elif item["type"] == "tool_end":
                    yield "event: tool_end\ndata: " + _json.dumps(
                        {"name": item["name"], "result": item["result"]},
                        ensure_ascii=False) + "\n\n"
                    steps.append({"name": item["name"], "args": item.get("args"), "result": item["result"]})
                    tool_names.append(item["name"])
                elif item["type"] == "token":
                    full_answer.append(item["text"])
                    yield "event: token\ndata: " + _json.dumps(
                        {"text": item["text"]}, ensure_ascii=False) + "\n\n"
            answer = "".join(full_answer)
            _append_turn(session_id, req.question, answer)
            # 落库：完整对话（含工具步骤与数据来源标注），供侧边栏与刷新恢复
            msgs = _load_messages(session_id)
            msgs.append({"role": "user", "text": req.question, "ts": time.time()})
            msgs.append({"role": "bot", "text": answer, "steps": steps, "notes": _derive_notes(tool_names), "ts": time.time()})
            _save_messages(session_id, client_id, msgs)
            yield "event: done\ndata: {}\n\n"
        except Exception as e:  # noqa
            yield "event: error\ndata: " + _json.dumps(
                {"message": str(e)[:200]}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ---------- 侧边栏历史对话接口（按 client_id 隔离） ----------

@app.get("/conversations")
async def list_conversations(client_id: str = Query("")):
    try:
        with _DB_LOCK:
            conn = _db()
            rows = conn.execute(
                "SELECT id, title, updated_at FROM conversations WHERE client_id=? "
                "ORDER BY updated_at DESC", (client_id,)).fetchall()
            conn.close()
        return {"conversations": [
            {"id": r["id"], "title": r["title"] or "新对话", "updated_at": r["updated_at"]}
            for r in rows
        ]}
    except Exception:
        return {"conversations": []}


@app.get("/conversations/{cid}")
async def get_conversation(cid: str, client_id: str = Query("")):
    try:
        with _DB_LOCK:
            conn = _db()
            row = conn.execute(
                "SELECT id, title, messages FROM conversations WHERE id=? AND client_id=?",
                (cid, client_id)).fetchone()
            conn.close()
        if not row:
            return {"id": cid, "title": "", "messages": []}
        return {"id": row["id"], "title": row["title"] or "新对话",
                "messages": _json.loads(row["messages"] or "[]")}
    except Exception:
        return {"id": cid, "title": "", "messages": []}


@app.delete("/conversations/{cid}")
async def delete_conversation(cid: str, client_id: str = Query("")):
    try:
        with _DB_LOCK:
            conn = _db()
            conn.execute("DELETE FROM conversations WHERE id=? AND client_id=?", (cid, client_id))
            conn.commit()
            conn.close()
    except Exception:
        pass
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/download/runtime")
async def download_runtime():
    """提供最新运行时压缩包下载（供本地部署同步）"""
    matches = sorted(glob.glob(os.path.join(BASE_DIR, "ai-travel-agent-runtime-*.zip")), reverse=True)
    if not matches:
        return {"error": "未找到运行包，请重新打包"}
    return FileResponse(matches[0], filename="ai-travel-agent-runtime.zip")


@app.get("/")
async def index():
    # 前端单页入口频繁迭代，强制不缓存，避免样式/脚本更新后用户看到旧版
    return FileResponse(
        os.path.join(BASE_DIR, "static", "index.html"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT") or 8000)  # 容错：PORT 未设置/为空时回退到 8000
    uvicorn.run(app, host="0.0.0.0", port=port)
