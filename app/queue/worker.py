"""
عامل الخلفية (Background Worker) - يستهلك المهام من طوابير Upstash Redis
يعمل كحلقة (Loop) مستقلة داخل نفس عملية FastAPI (asyncio.create_task)
بهدف عدم حجب webhook الخاص بتيليجرام أثناء استدعاء Gemini أو توليد الملفات
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile

from app.bot.keyboards import admin_review_keyboard, docx_offer_keyboard
from app.config import settings
from app.queue.redis_client import dequeue_blocking, enqueue
from app.services import gemini_service
from app.services.db import db
from app.services.docx_generator import generate_cv_docx
from app.services.pdf_generator import generate_cv_pdf

logger = logging.getLogger(__name__)


async def _process_cv_generation(bot: Bot, payload: dict[str, Any]) -> None:
    chat_id = payload["chat_id"]
    user_id = payload["user_id"]
    raw_text = payload["raw_text"]
    language = payload.get("language", "ar")

    try:
        cv_data = await gemini_service.extract_cv_from_text(raw_text, language=language)
    except Exception:
        logger.exception("فشل استخراج بيانات السيرة الذاتية عبر Gemini")
        await bot.send_message(
            chat_id,
            "⚠️ حدث خطأ أثناء تحليل بياناتك. من فضلك حاول مرة أخرى بعد قليل.",
        )
        return

    try:
        pdf_bytes = generate_cv_pdf(cv_data)
    except Exception:
        logger.exception("فشل توليد ملف PDF")
        await bot.send_message(chat_id, "⚠️ حدث خطأ أثناء توليد ملف PDF. حاول مرة أخرى.")
        return

    cv_id = await db.insert_cv_record(user_id=user_id, parsed_json=cv_data, fmt="pdf")

    file_name = f"CV_{cv_data.get('full_name', 'candidate').replace(' ', '_')}.pdf"
    pdf_caption = (
        "✅ هذه نسختك المجانية من السيرة الذاتية بصيغة PDF."
        if language == "ar"
        else "✅ Here is your free PDF CV."
    )
    docx_prompt = (
        "هل ترغب أيضاً بنسخة Word قابلة للتعديل يمكنك تحديثها بنفسك لاحقاً؟"
        if language == "ar"
        else "Would you also like an editable Word version you can update yourself later?"
    )
    await bot.send_document(
        chat_id=chat_id,
        document=BufferedInputFile(pdf_bytes, filename=file_name),
        caption=pdf_caption,
    )
    await bot.send_message(chat_id, docx_prompt, reply_markup=docx_offer_keyboard(cv_id))


async def _process_receipt_screening(bot: Bot, payload: dict[str, Any]) -> None:
    payment_id = payload["payment_id"]
    chat_id = payload["chat_id"]
    user_id = payload["user_id"]
    receipt_file_id = payload["receipt_file_id"]

    try:
        file = await bot.get_file(receipt_file_id)
        file_bytes_io = await bot.download_file(file.file_path)
        image_bytes = file_bytes_io.read()
    except Exception:
        logger.exception("فشل تحميل صورة الإيصال من تيليجرام")
        await bot.send_message(chat_id, "⚠️ تعذر تحميل الصورة، من فضلك أعد رفعها.")
        return

    try:
        verdict = await gemini_service.verify_payment_receipt(image_bytes)
    except Exception:
        logger.exception("فشل فحص الإيصال عبر Gemini Vision - سيُرسل للمراجعة اليدوية مباشرة")
        verdict = {"is_receipt": True, "confidence": 0.0, "reason": "تعذّر الفحص الآلي، مراجعة يدوية مطلوبة"}

    if not verdict.get("is_receipt", False) and verdict.get("confidence", 0) >= 0.6:
        await db.update_payment_status(payment_id, "rejected", admin_note="الصورة ليست إيصال دفع صالح")
        await bot.send_message(
            chat_id,
            "❌ الصورة المرسلة لا تبدو كإيصال دفع صالح. من فضلك أرسل صورة إيصال Sham Cash واضحة.",
        )
        return

    # إحالة الإيصال إلى مجموعة الأدمن للمراجعة النهائية
    payment = await db.get_payment(payment_id)
    if payment is None:
        logger.error("لم يتم العثور على سجل الدفعة %s", payment_id)
        return

    caption = (
        f"🧾 *طلب دفع جديد* #{payment_id}\n"
        f"👤 معرّف المستخدم: `{user_id}`\n"
        f"💵 المبلغ: {payment['amount']} {payment['currency']}\n"
        f"🤖 تقييم الفحص الآلي: {verdict.get('reason', '-')} (ثقة {verdict.get('confidence', 0):.2f})"
    )
    await bot.send_photo(
        chat_id=settings.admin_group_id,
        photo=receipt_file_id,
        caption=caption,
        parse_mode="Markdown",
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
        docx_bytes = generate_cv_docx(cv_record["parsed_json"])
    except Exception:
        logger.exception("فشل توليد ملف DOCX")
        await bot.send_message(user_id, "⚠️ حدث خطأ أثناء توليد ملف Word. تواصل مع الدعم من فضلك.")
        return

    await db.insert_cv_record(user_id=user_id, parsed_json=cv_record["parsed_json"], fmt="docx")

    language = cv_record["parsed_json"].get("_language", "ar")
    file_name = f"CV_{cv_record['parsed_json'].get('full_name', 'candidate').replace(' ', '_')}.docx"
    docx_caption = (
        "🎉 تفضّل نسختك القابلة للتعديل بصيغة Word!"
        if language == "ar"
        else "🎉 Here is your editable Word version!"
    )
    await bot.send_document(
        chat_id=user_id,
        document=BufferedInputFile(docx_bytes, filename=file_name),
        caption=docx_caption,
    )


_HANDLERS = {
    "cv": _process_cv_generation,
    "receipt": _process_receipt_screening,
    "docx": _process_docx_generation,
}


async def _consume_queue(bot: Bot, queue_name: str, kind: str) -> None:
    while True:
        try:
            payload = await dequeue_blocking(queue_name, timeout=30)
            if payload is None:
                continue
            await _HANDLERS[kind](bot, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("خطأ غير متوقع في عامل الطابور %s", queue_name)
            await asyncio.sleep(5)


async def start_workers(bot: Bot) -> list[asyncio.Task]:
    """يشغّل عمّال الطوابير الثلاثة كـ asyncio.Task مستقلة."""
    tasks = [
        asyncio.create_task(_consume_queue(bot, settings.queue_cv_generation, "cv")),
        asyncio.create_task(_consume_queue(bot, settings.queue_receipt_screening, "receipt")),
        asyncio.create_task(_consume_queue(bot, settings.queue_docx_generation, "docx")),
    ]
    return tasks