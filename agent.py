# -*- coding: utf-8 -*-
"""
LangGraph ReAct Agent 编排层
- 通过 langchain-mcp-adapters 连接天气 / 地图两个 MCP Server（stdio）
- 大模型：通义千问（DashScope OpenAI 兼容端点）
- 暴露 async run_query(question) -> (final_answer, tool_calls)
"""
import os
import sys
import json
import asyncio
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import ToolMessage

load_dotenv()

# 用当前 python 解释器的绝对路径启动 MCP stdio 子进程，
# 避免双击 bat / 无 PATH 环境时找不到 python 命令。
_PYTHON = sys.executable

MCP_SERVERS = {
    "weather": {"command": _PYTHON, "args": ["weather_server.py"], "transport": "stdio"},
    "map": {"command": _PYTHON, "args": ["map_server.py"], "transport": "stdio"},
    "train": {"command": _PYTHON, "args": ["train_server.py"], "transport": "stdio"},
}

SYSTEM_PROMPT = """你是一个专业的智能出行助手，具备以下能力：
1. 天气查询：query_weather（支持城市与日期，城市可用中文）。
2. 地图查询：maps_search 查地点位置，maps_direction 规划两地路线。
3. 高铁规划：query_train 查两城市间高铁方案（内置已核实高铁路网，含新建车站）。
4. 多工具协同：需求同时涉及天气/地点/路线/高铁时，分别调用对应工具后综合回答。

使用指南：
- 天气单日/多日区分：用户说"今天/明天/具体日期"只查当天（days=1 或省略）；只有"最近/未来几天/最近有雨吗"才传 days。
- 城市切换必须重查：任何城市名（含"东京呢""上海呢"等省略）都须重新调用 query_weather，严禁复用其他城市数据或把 A 市天气套到 B 市。
- 地点搜索保留完整复合地名："北京天安门""上海虹桥站"须传含城市的完整地名，勿只传"天安门""虹桥站"。
- 多工具任务做完整：天气+出行须同时调 query_weather 与 maps_direction（及 query_train），不漏项。
- 同轮回合避免冗余重复调用：相同参数已查过就直接复用，不再调。
- 泛称日期（今天/明天/后天/大后天/下周X/周末）原样传给 date 参数即可，系统会自动解析为绝对日期；回答日期须与工具返回一致。
- 信息不足（未说明城市、路线缺起点/终点）主动追问，勿臆测。
- 路线须基于 maps_direction 返回的真实路段（道路名+转向）描述，勿编造"向东/向西"。
- get_weather_tips(season) 仅当用户明确索要"季节贴士/穿衣建议"时调用；常规查天气用 query_weather，勿冗余加调。
- 高铁用 query_train(from_station, to_station)，返回"线路参考"，实时车次/票价/余票以 12306 为准，回答须说明。
- 重要事实（须基于工具，勿用旧知识）：威远站 2023 年底随成自宜高铁开通，威远已通高铁；东莞有虎门站、东莞南站等。
- 跨国（东京/洛杉矶等）：高铁无法直达，须告知乘飞机，勿给国内高铁/驾车方案。
- 国际大都市白名单：东京/大阪/首尔/纽约/洛杉矶/伦敦/巴黎等统一理解为对应国家城市，严禁映射到国内同名小地点；海外城市天气用 query_weather 查询。
- 路线省略问法（"深圳到北京呢""成都呢"）系统会自动补全为完整独立查询，请务必调用 query_train，勿依赖/复用历史、勿凭记忆回答。
- 出差意图延续：先问"从A去B出差看天气和路线"、再问"去C呢"时，须同时调 query_weather(C)、query_train(A,C)、maps_direction(A,C) 三项后综合，缺一项算错。
- 出行问法缺出发地/目的地时，若最近几轮已明确对应城市，可直接复用，勿重复追问。
- 省/直辖市未指定城市时默认按省会查询，并说明"默认按省会XX查询"。
- 连续相同查询且已答过，直接引用上一轮，勿重复调工具。
- 火车未收录城市兜底：query_train 返回"未收录"时，立即用 maps_direction 查 A→该城市驾车方案作备选，并说明火车数据未覆盖。
请以友好、专业的方式回复。"""


def get_model() -> ChatOpenAI:
    """LLM 可切换：默认 DeepSeek，LLM_PROVIDER=qwen 时回退通义千问。
    DeepSeek / 通义千问均走 OpenAI 兼容端点，工具调用(function calling)一致。"""
    provider = os.getenv("LLM_PROVIDER", "deepseek").lower()
    if provider == "qwen":
        return ChatOpenAI(
            model=os.getenv("QWEN_MODEL", "qwen-plus"),
            api_key=os.getenv("DASHSCOPE_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            temperature=0,
        )
    # 默认 DeepSeek（OpenAI 兼容）：https://api.deepseek.com
    return ChatOpenAI(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com",
        temperature=0,
    )


def _norm_args(args):
    """将工具参数归一化为稳定的、顺序无关的字符串，用于同轮去重。"""
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return repr(args)


def _resolve_date_for_agent(text: str | None, today: date) -> str | None:
    """把相对日期统一解析为 YYYY-MM-DD 绝对日期，避免不同工具各自解析导致不一致。

    支持：今天/明天/后天/大后天/下周X/周末；已经是绝对日期或无法识别则原样返回。
    """
    if not text:
        return text
    t = text.strip()
    table = {"今天": 0, "今日": 0, "明天": 1, "后天": 2, "大后天": 3,
             "today": 0, "tomorrow": 1, "day after tomorrow": 2}
    if t in table:
        return (today + timedelta(days=table[t])).isoformat()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            datetime.strptime(t, fmt).date()
            return t
        except ValueError:
            continue
    if t.startswith("下周"):
        # 「下周」= 下一个自然周（周一~周日）。锚点取"下一个周一"，且保证严格落在下一周
        # （今天若为周一，则取 7 天后的周一），**不要再额外 +7**，否则会整体落到"下下周"。
        next_mon = (0 - today.weekday()) % 7
        if next_mon == 0:
            next_mon = 7
        weekday_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        if len(t) > 2:
            wd = weekday_map.get(t[-1])
            if wd is not None:
                # 下周X = 下周一 + 该星期在当前周内的偏移
                return (today + timedelta(days=next_mon + wd)).isoformat()
        return (today + timedelta(days=next_mon)).isoformat()
    if "周末" in t:
        days_ahead = (5 - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return (today + timedelta(days=days_ahead)).isoformat()
    return text


def _make_cached_tool(tool, cache, today: date):
    """把 MCP 工具包一层同轮回合缓存：相同 (工具名, 参数) 的调用只真正执行一次，
    其余直接返回缓存结果，避免多工具轮里冗余重查（如重复调 query_weather）浪费外部调用。

    返回的 StructuredTool 复用原工具的 name / description / args_schema，仅替换执行体。
    """
    from langchain_core.tools import StructuredTool

    async def _invoke(**kwargs):
        # 统一日期解析：把相对日期转成绝对日期后再调用工具，避免 weather/train 各自解析不一致
        if "date" in kwargs and kwargs["date"]:
            kwargs["date"] = _resolve_date_for_agent(kwargs["date"], today)
        key = (tool.name, _norm_args(kwargs))
        if key in cache:
            return cache[key]
        result = await tool.ainvoke(kwargs)
        cache[key] = result
        return result

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=getattr(tool, "args_schema", None),
        coroutine=_invoke,
    )


# ---- MCP 客户端持久化（避免每次请求都拉起 3 个 stdio 子进程）----
# 原实现每次 run_query 都 new MultiServerMCPClient + get_tools()，会为每个请求
# 重新拉起 weather/map/train 三个 Python 子进程（实测稳定 ~1.6s/请求），且子进程
# 在函数返回后未退出造成泄漏。改为模块级惰性单例：首次调用初始化一次，之后所有
# 请求复用同一组已连接的工具对象，进程级只保留这一组子进程。
_mcp_client = None
_mcp_tools = None
_mcp_lock = asyncio.Lock()


async def _get_mcp_tools():
    """返回持久化的 MCP 工具列表（进程内仅初始化一次）。"""
    global _mcp_client, _mcp_tools
    if _mcp_tools is not None:
        return _mcp_tools
    async with _mcp_lock:  # 并发首访时只初始化一次，避免重复拉起
        if _mcp_tools is None:
            client = MultiServerMCPClient(MCP_SERVERS)
            tools = await client.get_tools()
            _mcp_client = client
            _mcp_tools = tools
    return _mcp_tools


async def run_query(question: str, history=None):
    """运行一次 Agent 查询，返回 (最终回答, 工具调用列表, 工具步骤明细)。

    支持多轮对话：history 为 [(role, content), ...] 列表，会拼接到当前问题前。
    工具步骤 steps 为 [{name, args, result}]，用于前端可视化展示
    Agent 调用了哪些工具、传了什么参数、拿到了什么结果。
    """
    messages = list(history) if history else []
    messages.append(("user", question))
    # 复用持久化的 MCP 工具连接（不再每次请求拉起 3 个子进程，实测省 ~1.6s/请求）
    raw_tools = await _get_mcp_tools()
    # 同轮回合缓存：相同 (工具名, 参数) 的重复调用直接复用，不重复执行外部工具。
    # 注意 _call_cache 每请求新建，只负责单轮回合内去重，与连接生命周期无关。
    request_today = date.today()
    _call_cache: dict = {}
    tools = [_make_cached_tool(t, _call_cache, request_today) for t in raw_tools]
    agent = create_react_agent(get_model(), tools, prompt=SYSTEM_PROMPT)
    result = await agent.ainvoke({"messages": messages})
    messages = result["messages"]
    # 工具结果按 tool_call_id 索引，便于与 tool_calls 配对
    tool_results = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            tool_results[m.tool_call_id] = m.content
    # 收集本轮回合的全部工具调用（按出现顺序）
    raw = []
    for m in messages:
        if getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                raw.append({"name": tc["name"], "args": tc["args"], "id": tc["id"]})
    # 同轮去重：相同 (工具名, 归一化参数) 的重复调用只保留首次，
    # 避免模型在多工具轮里冗余重查（如重复调 query_weather）既浪费调用又干扰评测口径。
    seen = set()
    deduped = []
    for item in raw:
        key = (item["name"], _norm_args(item["args"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    tool_calls = []
    steps = []
    for item in deduped:
        tool_calls.append({"name": item["name"], "args": item["args"]})
        steps.append({
            "name": item["name"],
            "args": item["args"],
            "result": tool_results.get(item["id"], ""),
        })
    final = messages[-1].content if messages else ""
    return final, tool_calls, steps


async def run_query_stream(question: str, history=None):
    """流式版本：通过 LangGraph astream_events 逐步产出「工具步骤 + 答案 token」。

    用于 /chat/stream（SSE），目标是提升「感知延迟」——用户能立刻看到工具被调用、
    答案逐字流出，而不是干等数秒后整段蹦出。正确性逻辑与 run_query 完全一致
    （同样的持久化 MCP 工具、同轮回合去重、同一套 System Prompt）。
    产出事件（dict）：
      {"type": "tool_start", "name", "args"}   工具开始调用
      {"type": "tool_end",   "name", "result"} 工具返回结果
      {"type": "token",      "text"}           答案的一个文本片段
    """
    messages = list(history) if history else []
    messages.append(("user", question))
    # 复用持久化的 MCP 工具连接（与 run_query 同一套）
    raw_tools = await _get_mcp_tools()
    request_today = date.today()
    _call_cache: dict = {}
    tools = [_make_cached_tool(t, _call_cache, request_today) for t in raw_tools]
    agent = create_react_agent(get_model(), tools, prompt=SYSTEM_PROMPT)

    seen_start = set()  # 本次流式会话内已推送的 (name,args)，避免 astream_events 重复事件
    seen_end = set()    # 本次流式会话内已推送的 (name,result)
    async for ev in agent.astream_events({"messages": messages}, version="v2"):
        et = ev.get("event")
        if et == "on_tool_start":
            key = (ev.get("name", ""),
                   json.dumps(ev.get("data", {}).get("input", {}),
                              sort_keys=True, ensure_ascii=False))
            if key in seen_start:
                continue
            seen_start.add(key)
            yield {
                "type": "tool_start",
                "name": ev.get("name", ""),
                # 兼容部分 langchain-core 版本 on_tool_start 的 data.input 为 None 的情况，
                # 避免 SSE 推 null 导致前端工具卡片显示「参数: null」
                "args": ev.get("data", {}).get("input") or {},
            }
        elif et == "on_tool_end":
            out = ev.get("data", {}).get("output")
            result = getattr(out, "content", out)
            rkey = (ev.get("name", ""), repr(result))
            if rkey in seen_end:
                continue
            seen_end.add(rkey)
            yield {"type": "tool_end", "name": ev.get("name", ""), "result": result}
        elif et == "on_chat_model_stream":
            chunk = ev.get("data", {}).get("chunk")
            if chunk is None:
                continue
            content = getattr(chunk, "content", None)
            tcc = getattr(chunk, "tool_call_chunks", None)
            # 只流式输出「最终答案轮」的文本：跳过带工具调用块的 chunk
            if content and not tcc:
                text = "".join(
                    c.get("text", "") for c in content if isinstance(c, dict)
                ) if isinstance(content, list) else str(content)
                if text:
                    yield {"type": "token", "text": text}
