"""
عميل Redis غير المتزامن (Upstash) المستخدم كطابور مهام (Task Queue) بسيط
نستخدم أوامر LPUSH / BRPOP القياسية عبر redis-py (وضع asyncio)
"""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from app.config import settings

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.upstash_redis_url,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=10,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None


async def enqueue(queue_name: str, payload: dict[str, Any]) -> None:
    """إضافة مهمة جديدة إلى نهاية الطابور"""
    r = get_redis()
    await r.lpush(queue_name, json.dumps(payload, ensure_ascii=False))


async def dequeue_blocking(queue_name: str, timeout: int = 5) -> dict[str, Any] | None:
    """
    سحب مهمة من بداية الطابور مع الحظر (Blocking) حتى مهلة محددة.
    تُستخدم داخل حلقة العامل (Worker Loop).
    """
    r = get_redis()
    result = await r.brpop([queue_name], timeout=timeout)
    if result is None:
        return None
    _, raw_payload = result
    return json.loads(raw_payload)
