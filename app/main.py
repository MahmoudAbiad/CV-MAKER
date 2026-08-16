"""
نقطة تشغيل التطبيق - FastAPI + aiogram 3.x (Webhook Mode)
مُحسَّن للعمل ضمن قيود Render Free Tier (0.1 vCPU / 512MB RAM)
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.bot.handlers import admin_router, user_router
from app.bot.middlewares import AdminOnlyMiddleware, UserRegistrationMiddleware
from app.config import settings
from app.queue.redis_client import close_redis
from app.services.db import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("cv_bot")

bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ترتيب التسجيل: راوتر الأدمن أولاً كي تُعالَج حالاته (FSM) قبل المعالج العام للنصوص
admin_router.message.middleware(AdminOnlyMiddleware())
admin_router.callback_query.middleware(AdminOnlyMiddleware())

dp.include_router(admin_router)
dp.include_router(user_router)

dp.update.middleware(UserRegistrationMiddleware())

_worker_tasks: list[asyncio.Task] = []

app = FastAPI(title="CV Generator Bot")


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


@app.on_event("startup")
async def on_startup() -> None:
    from app.queue.worker import start_workers

    # 1. الاتصال بقاعدة البيانات بشكل آمن
    try:
        await db.connect()
        logger.info("✅ تم الاتصال بقاعدة بيانات Turso")
    except Exception:
        logger.exception("⚠️ فشل الاتصال الأولي بقاعدة بيانات Turso")

    # 2. تسجيل الـ Webhook
    webhook_url = settings.webhook_base_url.rstrip("/") + settings.webhook_path
    await _setup_webhook_safe(webhook_url)

    # 3. تشغيل عمال الطوابير في الخلفية
    global _worker_tasks
    try:
        _worker_tasks = await start_workers(bot)
        logger.info("✅ تم تشغيل %s عامل خلفية (Background Workers)", len(_worker_tasks))
    except Exception:
        logger.exception("⚠️ فشل بدء عمال الخلفية")


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for task in _worker_tasks:
        task.cancel()
    await asyncio.gather(*_worker_tasks, return_exceptions=True)

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

    logger.info("تم إيقاف التطبيق بنجاح")


@app.get("/")
async def health_check() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "cv-generator-bot"})


@app.post(settings.webhook_path)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    if x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})

    # تغذية التحديث للـ Dispatcher وإرجاع 200 فوراً لتيليغرام
    await dp.feed_update(bot=bot, update=update)
    return JSONResponse({"ok": True})