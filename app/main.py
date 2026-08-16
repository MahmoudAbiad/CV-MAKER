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

# ترتيب التسجيل مهم: راوتر الأدمن أولاً كي تُعالَج حالاته (FSM) قبل
# المعالج العام للنصوص في راوتر المستخدم
admin_router.message.middleware(AdminOnlyMiddleware())
admin_router.callback_query.middleware(AdminOnlyMiddleware())

dp.include_router(admin_router)
dp.include_router(user_router)

dp.update.middleware(UserRegistrationMiddleware())

_worker_tasks: list[asyncio.Task] = []

app = FastAPI(title="CV Generator Bot")


@app.on_event("startup")
async def on_startup() -> None:
    from app.queue.worker import start_workers

    await db.connect()

    webhook_url = settings.webhook_base_url.rstrip("/") + settings.webhook_path
    await bot.set_webhook(
        url=webhook_url,
        secret_token=settings.webhook_secret,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )
    logger.info("تم ضبط الـ Webhook على: %s", webhook_url)

    global _worker_tasks
    _worker_tasks = await start_workers(bot)
    logger.info("تم تشغيل %s عامل خلفية (Background Workers)", len(_worker_tasks))


@app.on_event("shutdown")
async def on_shutdown() -> None:
    for task in _worker_tasks:
        task.cancel()
    await asyncio.gather(*_worker_tasks, return_exceptions=True)

    await bot.delete_webhook()
    await bot.session.close()
    await db.close()
    await close_redis()
    logger.info("تم إيقاف التطبيق بنجاح")


@app.get("/")
async def health_check() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "cv-generator-bot"})


@app.post(settings.webhook_path)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    # التحقق من الـ secret token لمنع الطلبات المزيّفة (Webhook Security)
    if x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})

    # معالجة سريعة دون حجب الاستجابة - نُعيد 200 فوراً لتيليجرام
    await dp.feed_update(bot=bot, update=update)
    return JSONResponse({"ok": True})
