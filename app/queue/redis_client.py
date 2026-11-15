"""
عميل Redis غير المتزامن (Upstash) المستخدم كطابور مهام (Task Queue)
مُعزز بإعادة الاتصال التلقائي وTCP Keepalive لمعالجة انقطاعات Serverless
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError, RedisError

from app.config import settings

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None
_redis_lock = asyncio.Lock()


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.upstash_redis_url,
            decode_responses=True,
            socket_timeout=35.0,
            socket_connect_timeout=15.0,
            socket_keepalive=True,
            health_check_interval=25,
            retry_on_timeout=True,
            max_connections=5,
        )
    return _redis


async def reset_redis() -> None:
    """إغلاق الاتصال الحالي وإعادة ضبط الكائن بأمان مع قفل لتجنب حالات السباق."""
    global _redis
    async with _redis_lock:
        if _redis is not None:
            try:
                await _redis.aclose()
            except Exception:
                pass
            _redis = None


async def close_redis() -> None:
    await reset_redis()


async def enqueue(queue_name: str, payload: dict[str, Any]) -> None:
    """إضافة مهمة جديدة إلى نهاية الطابور مع معالجة إعادة المحاولة لمرة واحدة"""
    try:
        r = get_redis()
        await r.lpush(queue_name, json.dumps(payload, ensure_ascii=False))
    except (RedisConnectionError, RedisTimeoutError, OSError):
        logger.warning("تعذر الإرسال إلى Redis، جاري إعادة المحاولة بعد تجديد الاتصال...")
        await reset_redis()
        await asyncio.sleep(1.0)
        r = get_redis()
        await r.lpush(queue_name, json.dumps(payload, ensure_ascii=False))


async def dequeue_blocking(queue_name: str, timeout: int = 30) -> dict[str, Any] | None:
    """سحب مهمة من طابور مفرد (متوافق مع الاستخدامات السابقة)."""
    res = await dequeue_multi_blocking([queue_name], timeout=timeout)
    if res is None:
        return None
    return res[1]


async def dequeue_multi_blocking(queues: list[str], timeout: int = 30) -> tuple[str, dict[str, Any]] | None:
    """
    استهلاك متعدد الطوابير بأمر واحد وبأولوية صارمة.
    يوفر أكثر من 66% من طلبات Redis اليومية في وضع الخمول لحماية كوتة Upstash.
    """
    try:
        r = get_redis()
        result = await r.brpop(queues, timeout=timeout)
        if result is None:
            return None
        queue_name, raw_payload = result
        return queue_name, json.loads(raw_payload)
    except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
        logger.warning("انقطاع أو مهلة في اتصال Redis: %s (انتظار ثانيتين لتفادي استنزاف الموارد)", exc)
        await reset_redis()
        await asyncio.sleep(2.0)
        return None
    except json.JSONDecodeError as exc:
        logger.error("رسالة غير صالحة JSON من الطابور: %s", exc)
        return None
    except RedisError as exc:
        logger.error("خطأ عام من عميل Redis: %s (انتظار ثانيتين لتفادي استنزاف الموارد)", exc)
        await reset_redis()
        await asyncio.sleep(2.0)
        return None
