"""
عميل Redis غير المتزامن (Upstash) المستخدم كطابور مهام (Task Queue)
مُعزز بإعادة الاتصال التلقائي وTCP Keepalive لمعالجة انقطاعات Serverless
"""
from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError, RedisError

from app.config import settings

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None


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
        )
    return _redis


async def reset_redis() -> None:
    """إغلاق الاتصال الحالي وإعادة ضبط الكائن لإجبار العميل على إعادة الاتصال"""
    global _redis
    if _redis is not None:
        try:
            await _redis.close()
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
    except (RedisConnectionError, RedisTimeoutError):
        logger.warning("تعذر الإرسال إلى Redis، جاري إعادة المحاولة بعد تجديد الاتصال...")
        await reset_redis()
        r = get_redis()
        await r.lpush(queue_name, json.dumps(payload, ensure_ascii=False))


async def dequeue_blocking(queue_name: str, timeout: int = 30) -> dict[str, Any] | None:
    """
    سحب مهمة من الطابور مع الحظر، مع معالجة انقطاعات TCP والتنسيقات غير الصالحة.
    """
    try:
        r = get_redis()
        result = await r.brpop([queue_name], timeout=timeout)
        if result is None:
            return None
        _, raw_payload = result
        return json.loads(raw_payload)
    except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
        logger.warning("انقطاع أو مهلة في اتصال Redis أثناء فحص الطابور %s: %s", queue_name, exc)
        await reset_redis()
        return None
    except json.JSONDecodeError as exc:
        logger.error("تم تجاهل رسالة تالفة (غير صالحة JSON) من الطابور %s: %s", queue_name, exc)
        return None
    except RedisError as exc:
        logger.error("خطأ عام من عميل Redis في الطابور %s: %s", queue_name, exc)
        return None
