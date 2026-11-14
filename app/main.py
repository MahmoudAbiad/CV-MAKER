"""
نقطة تشغيل التطبيق - FastAPI + aiogram 3.x (Webhook Mode)
مُحسَّن للعمل ضمن قيود Render Free Tier (0.1 vCPU / 512MB RAM)
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import secrets
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.bot.handlers import admin_router, user_router
from app.bot.middlewares import AdminOnlyMiddleware, ThrottlingMiddleware, UserRegistrationMiddleware
from app.config import settings
from app.queue.redis_client import close_redis
from app.services.db import db


class SensitiveDataFilter(logging.Filter):
    """فلتر أمني لحجب المفاتيح والتوكنات الحساسة تلقائياً من سجلات النظام (Logs)."""

    def __init__(self, secrets_to_mask: list[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets_to_mask if s and len(s) > 5]

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.msg, str):
            return True
        for secret in self.secrets:
            if secret in record.msg:
                record.msg = record.msg.replace(secret, "[REDACTED]")
            if record.args:
                # فحص الـ args إن كانت نصوصاً
                new_args = []
                for arg in record.args if isinstance(record.args, tuple) else [record.args]:
                    if isinstance(arg, str) and secret in arg:
                        new_args.append(arg.replace(secret, "[REDACTED]"))
                    else:
                        new_args.append(arg)
                record.args = tuple(new_args)
        return True


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("cv_bot")

# تحسين عتبات جامع القمامة (Garbage Collector) لتناسب بيئة 512MB RAM وتفريغ الذاكرة مبكراً
gc.set_threshold(400, 8, 8)

# تطبيق فلتر حجب البيانات الحساسة على جميع المعالجات
_secrets_list = [
    settings.bot_token,
    settings.gemini_api_key,
    settings.turso_auth_token,
    settings.webhook_secret,
]
sensitive_filter = SensitiveDataFilter(_secrets_list)
logging.getLogger().addFilter(sensitive_filter)

# مراجع قوية للمهام لتفادي جمعها بـ Garbage Collector في Python 3.11+
_background_tasks: set[asyncio.Task] = set()
_worker_tasks: list[asyncio.Task] = []

bot = Bot(
    token=settings.bot_token or "123456:dummy_token_for_tests",
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

admin_router.message.middleware(AdminOnlyMiddleware())
admin_router.callback_query.middleware(AdminOnlyMiddleware())

dp.include_router(admin_router)
dp.include_router(user_router)
dp.update.middleware(ThrottlingMiddleware())
dp.update.middleware(UserRegistrationMiddleware())


async def _setup_webhook_safe(webhook_url: str) -> None:
    """إعداد الـ Webhook مع محاولات إعادة الاتصال عند فشل الشبكة دون إسقاط السيرفر."""
    for attempt in range(1, 4):
        try:
            await bot.set_webhook(
                url=webhook_url,
                secret_token=settings.webhook_secret,
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
            )
            logger.info("✅ تم ضبط الـ Webhook بنجاح على: %s", webhook_url)
            return
        except Exception as exc:
            logger.warning("فشلت محاولة ضبط الـ Webhook (%s/3): %s", attempt, exc)
            await asyncio.sleep(2)
    logger.error("❌ تعذر ضبط الـ Webhook بعد 3 محاولات - تأكد من صلاحية الرابط والتوكن")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 0. التحقق من متغيرات البيئة قبل بدء أي خدمة
    try:
        settings.validate_required()
    except RuntimeError as e:
        logger.warning("تنبيه متغيرات البيئة: %s", e)

    from app.queue.worker import start_workers
    global _worker_tasks

    # 1. الاتصال بقاعدة البيانات
    if settings.turso_db_url and settings.turso_auth_token:
        try:
            await db.connect()
        except Exception:
            logger.exception("⚠️ فشل الاتصال الأولي بقاعدة بيانات Turso")

    # 2. تسجيل الـ Webhook
    if settings.webhook_base_url and settings.bot_token:
        webhook_url = settings.webhook_base_url.rstrip("/") + settings.webhook_path
        await _setup_webhook_safe(webhook_url)

    # 3. تشغيل عمال الخلفية
    if settings.upstash_redis_url:
        try:
            _worker_tasks = await start_workers(bot)
            logger.info("✅ تم تشغيل %s عامل خلفية (Background Workers)", len(_worker_tasks))
        except Exception:
            logger.exception("⚠️ فشل بدء عمال الخلفية")

    yield

    # --- مرحلة الإغلاق (Shutdown) ---
    logger.info("بدء إيقاف التطبيق وإغلاق الاتصالات...")

    for task in _worker_tasks:
        task.cancel()
    if _worker_tasks:
        await asyncio.gather(*_worker_tasks, return_exceptions=True)

    if _background_tasks:
        logger.info("انتظار اكتمال %d مهمة تحديث قيد المعالجة...", len(_background_tasks))
        done, pending = await asyncio.wait(_background_tasks, timeout=5.0)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        await bot.session.close()
    except Exception:
        pass

    try:
        await db.close()
    except Exception:
        pass

    try:
        await close_redis()
    except Exception:
        pass

    try:
        from app.services.gemini_service import close_http_client
        await close_http_client()
    except Exception:
        pass

    logger.info("✅ تم إيقاف التطبيق بنجاح")


app = FastAPI(title="CV Generator Bot", lifespan=lifespan)


@app.get("/")
async def health_check() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "cv-generator-bot"})


async def _feed_update_safe(update: Update) -> None:
    try:
        await dp.feed_update(bot=bot, update=update)
    except Exception:
        logger.exception("خطأ غير متوقع أثناء معالجة تحديث تيليجرام رقم %s", update.update_id)


@app.post(settings.webhook_path)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    # 1. التحقق الصارم في زمن ثابت (Constant-Time) من التوكن السري لمنع هجمات التوقيت
    if not settings.webhook_secret or not x_telegram_bot_api_secret_token:
        logger.warning("محاولة وصول غير مصرحة للـ Webhook (غياب التوكن السري)")
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not secrets.compare_digest(x_telegram_bot_api_secret_token, settings.webhook_secret):
        logger.warning("محاولة وصول غير مصرحة للـ Webhook (توكن سري غير متطابق)")
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 2. حماية الذاكرة ومنع هجمات حجب الخدمة (DoS) عبر تقييد حجم حمولة الطلب (Max 2MB)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > 2 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="Payload Too Large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")

    body_bytes = await request.body()
    if len(body_bytes) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Payload Too Large")

    try:
        data = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    update = Update.model_validate(data, context={"bot": bot})

    # إنشاء المهمة مع حفظ مرجع قوي لمنع الـ Garbage Collector من إنهائها مبكراً
    task = asyncio.create_task(_feed_update_safe(update))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return JSONResponse({"ok": True})
