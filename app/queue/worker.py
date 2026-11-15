"""
عامل الخلفية (Background Worker) - يستهلك المهام من طوابير Upstash Redis
يعمل كحلقة (Loop) مستقلة داخل نفس عملية FastAPI (asyncio.create_task)
بهدف عدم حجب webhook الخاص بتيليجرام أثناء استدعاء Gemini أو توليد الملفات
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile

from app.bot.keyboards import admin_review_keyboard, docx_offer_keyboard
from app.config import settings
from app.queue.redis_client import dequeue_blocking, dequeue_multi_blocking, enqueue
from app.services import gemini_service
from app.services.db import db
from app.services.docx_generator import generate_cv_docx
from app.services.pdf_generator import generate_cv_pdf

logger = logging.getLogger(__name__)


async def _process_cv_generation(bot: Bot, payload: dict[str, Any]) -> None:
    chat_id = payload["chat_id"]
    user_id = payload["user_id"]
    cv_data = payload["cv_data"]

    try:
        await bot.send_chat_action(chat_id, "upload_document")
    except Exception:
        pass

    try:
        # تشغيل التوليد في خيط منفصل لمنع حجب الـ Event Loop
        pdf_bytes = await asyncio.to_thread(generate_cv_pdf, cv_data)
    except Exception:
        logger.exception("فشل توليد ملف PDF")
        await bot.send_message(chat_id, "⚠️ حدث خطأ أثناء توليد ملف PDF. حاول مرة أخرى.")
        return

    cv_id: int | None = None
    try:
        cv_id = await db.insert_cv_record(user_id=user_id, parsed_json=cv_data, fmt="pdf")
    except Exception:
        logger.exception("فشل حفظ سجل السيرة الذاتية بقاعدة البيانات للمستخدم %s", user_id)

    safe_name = cv_data.get("full_name") or "candidate"
    safe_name = str(safe_name).replace(" ", "_")
    file_name = f"CV_{safe_name}.pdf"

    await bot.send_document(
        chat_id=chat_id,
        document=BufferedInputFile(pdf_bytes, filename=file_name),
        caption="🎉 <b>ألف مبروك! سيرتك الذاتية بصيغة PDF صارت جاهزة!</b>\n\nتمت صياغتها وتنسيقها بأعلى معايير الـ ATS العالمية. نتمنى لك كل التوفيق في مقابلاتك القادمة! 🍀✨",
        parse_mode="HTML",
    )

    if cv_id is not None:
        docx_prompt = (
            "💡 <b>بتحب تعدل بياناتك أو تضيف خبرات جديدة بالمستقبل؟</b>\n\n"
            "تقدر تحصل على نسخة <b>Word (DOCX)</b> المفتوحة لتعدل وتضيف عليها بأي وقت وبكل سهولة من موبايلك أو لابتوبك."
        )
        await bot.send_message(chat_id, docx_prompt, parse_mode="HTML", reply_markup=docx_offer_keyboard(cv_id))
    else:
        retry_msg = (
            "⚠️ تم إرسال ملف PDF بنجاح، لكن حدث خطأ مؤقت أثناء حفظ سجلك. "
            "إذا رغبت بنسخة Word لاحقاً تواصل مع الدعم."
        )
        await bot.send_message(chat_id, retry_msg)


def _is_valid_image(data: bytes) -> bool:
    """التحقق من توقيع البايتات السحرية (Magic Bytes) للصورة لمنع التلاعب بالملفات."""
    if len(data) < 16:
        return False
    # JPEG: FF D8 FF
    if data.startswith(b"\xff\xd8\xff"):
        return True
    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    # WEBP: RIFF....WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


async def _process_receipt_screening(bot: Bot, payload: dict[str, Any]) -> None:
    payment_id = payload["payment_id"]
    chat_id = payload["chat_id"]
    user_id = payload["user_id"]
    receipt_file_id = payload["receipt_file_id"]

    # 1. التحقق من إعادة استخدام نفس ملف الإيصال (Anti-Replay Attack)
    try:
        if await db.is_receipt_reused(receipt_file_id, exclude_payment_id=payment_id):
            await db.update_payment_status(payment_id, "rejected", admin_note="صورة الإيصال مستخدمة مسبقاً")
            await bot.send_message(
                chat_id,
                "❌ نعتذر، صورة هذا الإيصال تم استخدامها مسبقاً في عملية دفع أخرى ولا يمكن تكرارها.",
            )
            return
    except Exception:
        logger.exception("فشل فحص تكرار الإيصال في قاعدة البيانات")

    # 2. فحص حجم الملف وتنزيله بأمان لمنع هجمات حجب الخدمة (DoS)
    try:
        file = await bot.get_file(receipt_file_id)
        # الحد الأقصى المسموح به لحجم الصورة 5 ميغابايت
        max_size = 5 * 1024 * 1024
        if file.file_size and file.file_size > max_size:
            await db.update_payment_status(payment_id, "rejected", admin_note="حجم صورة الإيصال يتجاوز 5MB")
            await bot.send_message(
                chat_id,
                "⚠️ حجم الصورة كبير جداً (أكثر من 5 ميغابايت). يرجى إرسال لقطة شاشة بحجم مناسب.",
            )
            return

        file_bytes_io = await bot.download_file(file.file_path)
        image_bytes = file_bytes_io.read()
    except Exception:
        logger.exception("فشل تحميل صورة الإيصال من تيليجرام")
        await bot.send_message(chat_id, "⚠️ تعذر تحميل الصورة، من فضلك أعد رفعها.")
        return

    # 3. التحقق من سلامة البايتات وصيغة الصورة
    if not _is_valid_image(image_bytes):
        await db.update_payment_status(payment_id, "rejected", admin_note="الملف ليس صورة صالحة (JPEG/PNG/WEBP)")
        await bot.send_message(
            chat_id,
            "❌ الملف المرسل ليس صورة صالحة أو تالف. يرجى إرسال صورة إيصال واضحة بصيغة JPG أو PNG.",
        )
        return

    # 4. فحص الإيصال عبر الذكاء الاصطناعي
    try:
        verdict = await gemini_service.verify_payment_receipt(image_bytes)
    except Exception:
        logger.exception("فشل فحص الإيصال عبر Gemini Vision - سيُرسل للمراجعة اليدوية مباشرة")
        verdict = {"is_receipt": True, "confidence": 0.0, "reason": "تعذّر الفحص الآلي، مراجعة يدوية مطلوبة"}

    if not verdict.get("is_receipt", False) and verdict.get("confidence", 0) >= 0.6:
        await db.update_payment_status(payment_id, "rejected", admin_note="الصورة ليست إيصال دفع صالح")
        await bot.send_message(
            chat_id,
            "❌ الصورة المرسلة لا تبدو كإيصال دفع صالح. من فضلك أرسل صورة إيصال Sham Cash واضحة ومكتملة.",
        )
        return

    payment = await db.get_payment(payment_id)
    if payment is None:
        logger.error("لم يتم العثور على سجل الدفعة %s", payment_id)
        return

    tx_id = (verdict.get("transaction_id") or "").strip()
    amt = verdict.get("amount") or "-"
    recipient = verdict.get("recipient") or "-"

    # 5. الحماية من الإنفاق المزدوج عبر رقم المعاملة (Anti Double-Spending)
    if tx_id and tx_id not in ("-", "null", "none"):
        try:
            if await db.is_transaction_id_reused(tx_id, exclude_payment_id=payment_id):
                await db.update_payment_status(payment_id, "rejected", admin_note=f"رقم المعاملة {tx_id} مستخدم مسبقاً")
                await bot.send_message(
                    chat_id,
                    "❌ تم رفض الطلب: رقم العملية المالي الموضح في الإيصال تم استخدامه مسبقاً في عملية دفع سابقة.",
                )
                return
            await db.update_payment_transaction_id(payment_id, tx_id)
        except Exception:
            logger.exception("خطأ أثناء تدقيق وحفظ رقم المعاملة %s", tx_id)

    # 6. تنسيق آمن لإشعار الأدمن بصيغة HTML محصنة ضد الكسر وحقن Markdown
    safe_tx_id = html.escape(tx_id or "-")
    safe_amt = html.escape(str(amt))
    safe_recipient = html.escape(str(recipient))
    safe_reason = html.escape(str(verdict.get("reason", "-")))
    conf = verdict.get("confidence", 0.0) or 0.0

    caption = (
        f"🧾 <b>طلب دفع جديد #{payment_id}</b>\n"
        f"👤 معرّف المستخدم: <code>{user_id}</code>\n"
        f"💵 المبلغ المطلوب: {payment['amount']} {payment['currency']}\n"
        f"🔢 رقم المعاملة بالإيصال: <code>{safe_tx_id}</code>\n"
        f"💰 المبلغ المرصود: {safe_amt}\n"
        f"🎯 المستفيد: {safe_recipient}\n"
        f"🤖 تقييم الفحص: {safe_reason} (ثقة {conf:.2f})"
    )
    await bot.send_photo(
        chat_id=settings.admin_group_id,
        photo=receipt_file_id,
        caption=caption,
        parse_mode="HTML",
        reply_markup=admin_review_keyboard(payment_id),
    )
    await bot.send_message(chat_id, "🔍 تم إرسال الإيصال لفريق المراجعة، سنُعلمك بالنتيجة قريباً.")


async def _process_docx_generation(bot: Bot, payload: dict[str, Any]) -> None:
    user_id = payload["user_id"]
    cv_record_id = payload.get("cv_record_id")

    if cv_record_id is None:
        logger.error("لا يوجد cv_record_id مرتبط بالدفعة %s", payload.get("payment_id"))
        return

    cv_record = await db.get_cv_record(cv_record_id)
    if cv_record is None:
        logger.error("لم يتم العثور على سجل السيرة الذاتية %s", cv_record_id)
        return

    try:
        # تشغيل التوليد في خيط منفصل
        docx_bytes = await asyncio.to_thread(generate_cv_docx, cv_record["parsed_json"])
    except Exception:
        logger.exception("فشل توليد ملف DOCX")
        await bot.send_message(user_id, "⚠️ حدث خطأ أثناء توليد ملف Word. تواصل مع الدعم من فضلك.")
        return

    try:
        await db.insert_cv_record(user_id=user_id, parsed_json=cv_record["parsed_json"], fmt="docx")
    except Exception:
        logger.exception("فشل حفظ سجل نسخة DOCX بقاعدة البيانات للمستخدم %s", user_id)

    safe_name = cv_record["parsed_json"].get("full_name") or "candidate"
    safe_name = str(safe_name).replace(" ", "_")
    file_name = f"CV_{safe_name}.docx"

    await bot.send_document(
        chat_id=user_id,
        document=BufferedInputFile(docx_bytes, filename=file_name),
        caption="🎉 تفضّل نسختك القابلة للتعديل بصيغة Word!",
    )


# حصر عمليات توليد المستندات الثقيلة في مسار متسلسل لتجنب استنزاف 0.1 vCPU و 512MB RAM
_DOC_GENERATION_SEMAPHORE = asyncio.Semaphore(1)


async def _process_cv_generation_safe(bot: Bot, payload: dict[str, Any]) -> None:
    async with _DOC_GENERATION_SEMAPHORE:
        await _process_cv_generation(bot, payload)


async def _process_docx_generation_safe(bot: Bot, payload: dict[str, Any]) -> None:
    async with _DOC_GENERATION_SEMAPHORE:
        await _process_docx_generation(bot, payload)


_HANDLERS = {
    settings.queue_cv_generation: _process_cv_generation_safe,
    settings.queue_receipt_screening: _process_receipt_screening,
    settings.queue_docx_generation: _process_docx_generation_safe,
    # أسماء مرجعية معيارية للتوافق والاختبارات
    "cv": _process_cv_generation_safe,
    "receipt": _process_receipt_screening,
    "docx": _process_docx_generation_safe,
}


async def _unified_worker_loop(bot: Bot) -> None:
    queues = [
        settings.queue_cv_generation,
        settings.queue_receipt_screening,
        settings.queue_docx_generation,
    ]
    logger.info("بدء حلقة العامل الموحد (Unified Worker) للطوابير: %s", queues)

    while True:
        try:
            loop_start = asyncio.get_running_loop().time()
            item = await dequeue_multi_blocking(queues, timeout=30)
            if item is None:
                # إذا عادت الدالة في وقت وجيز جداً (أقل من نصف ثانية) دون مهام، فهذا يشير لانقطاع اتصال
                # نضمن وجود مهلة أمان لمنع أي حلقة تكرار سريعة (Busy Loop)
                elapsed = asyncio.get_running_loop().time() - loop_start
                if elapsed < 1.0:
                    await asyncio.sleep(2.0)
                continue
            queue_name, payload = item
            handler = _HANDLERS.get(queue_name)
            if handler:
                await handler(bot, payload)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("خطأ في العامل الموحد")
            await asyncio.sleep(3)


async def start_workers(bot: Bot) -> list[asyncio.Task]:
    """عامل موحد ذو أولوية لحفظ كوتة Upstash (توفير 66.7%) وخفض استهلاك RAM."""
    return [asyncio.create_task(_unified_worker_loop(bot))]
