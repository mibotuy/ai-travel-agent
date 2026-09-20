# -*- coding: utf-8 -*-
"""极简进程内 TTL 缓存。

用于缓存天气 / 地图工具对外部 API（高德、Open-Meteo）的调用结果，
降低重复查询与多轮追问的延迟。缓存按「函数 + 入参」维度隔离，进程内生效。

设计取舍：
- 缓存只针对稳定性高、短时不会变的外部结果（地理编码、天气、路线），命中即原样返回；
- TTL 分级：地理编码 7 天、天气 15 分钟、路线 30 分钟，避免返回过期数据；
- 不影响任何正确性逻辑（可靠性过滤、异常拦截等在缓存外层照常执行）。
"""
import time
import functools

_cache = {}  # key -> (expire_ts, value)


def ttl_cache(ttl_seconds, keyfn):
    """装饰异步函数，按 keyfn(*args, **kwargs) 做 TTL 缓存。

    keyfn 必须把入参映射为一个可字符串化的键；命中且在有效期内直接返回缓存值。
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            key = "cache:%s:%s:%s" % (
                fn.__module__, fn.__qualname__, str(keyfn(*args, **kwargs)),
            )
            now = time.time()
            entry = _cache.get(key)
            if entry is not None and entry[0] > now:
                return entry[1]
            value = await fn(*args, **kwargs)
            _cache[key] = (now + ttl_seconds, value)
            return value
        return wrapper
    return decorator
