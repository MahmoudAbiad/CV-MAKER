"""
عامل الخلفية (Background Worker) - يستهلك المهام من طوابير Upstash Redis
يعمل كحلقة (Loop) مستقلة داخل نفس عملية FastAPI (asyncio.create_task)
بهدف عدم حجب webhook الخاص بتيليجرام أثناء استدعاء Gemini أو توليد الملفات
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
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

    # فحص الكوتة المتاحة قبل استهلاك موارد المعالجة
    try:
        quota = await db.get_user_quota(user_id)
        if quota["total_available"] <= 0:
            logger.warning("المستخدم %s تجاوز حد الكوتة المسموح", user_id)
            quota_price = await db.get_cv_quota_price_syp()
            await bot.send_message(
                chat_id,
                f"⚠️ <b>لقد استنفدت رصيدك المتاح من السير الذاتية لهذا الشهر (10 سير).</b>\n\n"
                f"يمكنك الانتظار حتى بداية الشهر القادم أو شراء <b>باقة 10 سير إضافية</b> مقابل <b>{quota_price} ل.س</b>.",
            )
            return
    except Exception:
        logger.exception("فشل فحص كوتة المستخدم %s في عامل التوليد", user_id)

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

    # خصم سيرة واحدة من رصيد المستخدم
    try:
        await db.consume_user_quota(user_id)
    except Exception:
        logger.exception("فشل خصم كوتة المستخدم %s", user_id)

    cv_id: int | None = None
    try:
        cv_id = await db.insert_cv_record(user_id=user_id, parsed_json=cv_data, fmt="pdf")
    except Exception:
        logger.exception("فشل حفظ سجل السيرة الذاتية بقاعدة البيانات للمستخدم %s", user_id)

    raw_name = str(cv_data.get("full_name") or "candidate")
    clean_name = re.sub(r'[^\w\u0600-\u06FF\-]', '_', raw_name).strip("_")
    clean_name = clean_name[:40] or "candidate"
    file_name = f"CV_{clean_name}.pdf"

    caption_text = (
        "🎉 <b>ألف مبروك! سيرتك الذاتية بصيغة PDF صارت جاهزة!</b>\n\n"
        "تمت صياغتها وتنسيقها بأعلى معايير الـ ATS العالمية. نتمنى لك كل التوفيق في مقابلاتك القادمة! 🍀✨"
    )
    sent_successfully = False
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=BufferedInputFile(pdf_bytes, filename=file_name),
            caption=caption_text,
            parse_mode="HTML",
        )
        sent_successfully = True
    except Exception as exc1:
        logger.warning("فشل إرسال ملف PDF بالاسم المخصص (%s)، جاري المحاولة باسم افتراضي...", exc1)
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=BufferedInputFile(pdf_bytes, filename="CV.pdf"),
                caption="🎉 ألف مبروك! سيرتك الذاتية بصيغة PDF صارت جاهزة!",
            )
            sent_successfully = True
        except Exception as exc2:
            logger.exception("فشل نهائي في تسليم ملف PDF للمستخدم %s: %s", user_id, exc2)
            await bot.send_message(
                chat_id,
                "⚠️ واجهنا صعوبة مؤقتة في إرسال ملف الـ PDF عبر تيليجرام. يرجى الضغط على زر الاعتماد مجدداً أو مراجعة الدعم.",
            )

    if not sent_successfully:
        return

    if cv_id is not None:
        docx_usd, docx_syp = await db.get_docx_prices()
        docx_prompt = (
            "💡 <b>بتحب تعدل بياناتك أو تضيف خبرات جديدة بالمستقبل؟</b>\n\n"
            "تقدر تحصل على نسخة <b>Word (DOCX)</b> المفتوحة لتعدل وتضيف عليها بأي وقت وبكل سهولة من موبايلك أو لابتوبك."
        )
        await bot.send_message(
            chat_id,
            docx_prompt,
            parse_mode="HTML",
            reply_markup=docx_offer_keyboard(cv_id, price_usd=docx_usd, price_syp=docx_syp),
        )
    else:
        retry_msg = (
            "⚠️ تم إرسال ملف PDF بنجاح، لكن حدث خطأ مؤقت أثناء حفظ سجلك. "
            "إذا رغبت بنسخة Word لاحقاً تواصل مع الدعم."
        )
        await bot.send_message(chat_id, retry_msg)


def _detect_file_mime_type(data: bytes) -> str | None:
    """التحقق من توقيع البايتات السحرية (Magic Bytes) للصورة أو ملف PDF لمنع التلاعب."""
    if len(data) < 4:
        return None
    # JPEG: FF D8 FF
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    # WEBP: RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # PDF: %PDF قد يسبقه سطر فارغ أو تعليق في أول 1024 بايت بحسب المعيار الدولي ISO 32000-1
    if b"%PDF" in data[:1024]:
        return "application/pdf"
    return None


def _is_valid_image(data: bytes) -> bool:
    """التحقق من توقيع البايتات السحرية (Magic Bytes) للصورة أو الـ PDF للتوافق العكسي."""
    return _detect_file_mime_type(data) is not None


async def _process_receipt_screening(bot: Bot, payload: dict[str, Any]) -> None:
    payment_id = payload["payment_id"]
    chat_id = payload["chat_id"]
    user_id = payload["user_id"]
    receipt_file_id = payload["receipt_file_id"]
    is_pdf = bool(payload.get("is_pdf", False))

    # 1. التحقق من إعادة استخدام نفس ملف الإيصال (Anti-Replay Attack)
    try:
        if await db.is_receipt_reused(receipt_file_id, exclude_payment_id=payment_id):
            await db.update_payment_status(payment_id, "rejected", admin_note="صورة/ملف الإيصال مستخدم مسبقاً")
            await bot.send_message(
                chat_id,
                "❌ نعتذر، هذا الإيصال تم استخدامه مسبقاً في عملية دفع أخرى ولا يمكن تكراره.",
            )
            return
    except Exception:
        logger.exception("فشل فحص تكرار الإيصال في قاعدة البيانات")

    # 2. فحص حجم الملف وتنزيله بأمان لمنع هجمات حجب الخدمة (DoS)
    try:
        file = await bot.get_file(receipt_file_id)
        # الحد الأقصى المسموح به لحجم الملف 10 ميغابايت لدعم ملفات PDF والصور عالية الدقة
        max_size = 10 * 1024 * 1024
        if file.file_size and file.file_size > max_size:
            await db.update_payment_status(payment_id, "rejected", admin_note="حجم الإيصال يتجاوز 10MB")
            await bot.send_message(
                chat_id,
                "⚠️ حجم الملف كبير جداً (أكثر من 10 ميغابايت). يرجى إرسال ملف بحجم مناسب.",
            )
            return

        file_bytes_io = await bot.download_file(file.file_path)
        file_bytes = file_bytes_io.getvalue() if hasattr(file_bytes_io, "getvalue") else file_bytes_io.read()
    except Exception:
        logger.exception("فشل تحميل ملف الإيصال من تيليجرام")
        await bot.send_message(chat_id, "⚠️ تعذر تحميل الملف، من فضلك أعد إرساله.")
        return

    # 3. التحقق من سلامة البايتات وصيغة الملف (صورة أو PDF)
    detected_mime = _detect_file_mime_type(file_bytes)
    if not detected_mime:
        await db.update_payment_status(payment_id, "rejected", admin_note="الملف ليس صورة صالحة أو PDF")
        await bot.send_message(
            chat_id,
            "❌ الملف المرسل تالف أو ليس بصيغة مدعومة. يرجى إرسال صورة إيصال واضحة (JPG/PNG) أو ملف PDF صالح.",
        )
        return

    if detected_mime == "application/pdf":
        is_pdf = True

    # 4. فحص الإيصال عبر الذكاء الاصطناعي مع تمرير نوع الـ MIME الفعلي
    try:
        verdict = await gemini_service.verify_payment_receipt(file_bytes, mime_type=detected_mime)
    except Exception:
        logger.exception("فشل فحص الإيصال عبر Gemini - سيُرسل للمراجعة اليدوية مباشرة")
        verdict = {"is_receipt": True, "confidence": 0.0, "reason": "تعذّر الفحص الآلي، مراجعة يدوية مطلوبة"}

    if not verdict.get("is_receipt", False) and verdict.get("confidence", 0) >= 0.6:
        await db.update_payment_status(payment_id, "rejected", admin_note="الصورة ليست إيصال دفع صالح")
        await bot.send_message(
            chat_id,
            "❌ الصورة أو الملف المرسل لا تبدو كإيصال دفع صالح. من فضلك أرسل إيصال تحويل شام كاش واضح ومكتمل كصورة أو PDF.",
        )
        return

    payment = await db.get_payment(payment_id)
    if payment is None:
        logger.error("لم يتم العثور على سجل الدفعة %s", payment_id)
        return

    payment_type = payment.get("payment_type") or ("cv_quota" if payment.get("cv_record_id") is None else "docx")
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

    # تجهيز النصوص والبيانات الآمنة
    safe_tx_id = html.escape(tx_id or "-")
    safe_amt = html.escape(str(amt))
    safe_recipient = html.escape(str(recipient))
    safe_reason = html.escape(str(verdict.get("reason", "-")))
    conf = float(verdict.get("confidence", 0.0) or 0.0)
    is_valid_receipt = verdict.get("is_receipt") is True
    is_ai_generated = bool(verdict.get("is_ai_generated", False))
    file_type_badge = "📄 مستند PDF" if is_pdf else "🖼️ صورة إيصال"

    # قائمة وجهات الإدارة
    admin_targets: list[int] = []
    if settings.admin_group_id and settings.admin_group_id != 0:
        admin_targets.append(settings.admin_group_id)
    if settings.admin_user_ids:
        for aid in settings.admin_user_ids:
            if aid not in admin_targets and aid != 0:
                admin_targets.append(aid)

    # استرجاع سيرة ذاتية صالحة إن لم تكن مرتبطة بالدفعة مباشرة
    target_cv_id = payment.get("cv_record_id")
    if not target_cv_id:
        latest = await db.get_latest_cv_record(user_id)
        if latest:
            target_cv_id = latest["id"]

    # ------------------------------------------------------------------
    # 6. المصادقة التلقائية بالذكاء الاصطناعي لنسخة Word (DOCX) وباقات السير
    # - لنسخة Word: اعتماد فوري إذا كان الإيصال حقيقياً وغير مصطنع بالـ AI،
    #   أو في حال عدم وجود مشرفين مسجلين لتفادي حظر المستخدم نظراً لرمزية المبلغ.
    # - لباقة السير: اعتماد فوري عند نسبة ثقة عالية (>= 0.70)
    # ------------------------------------------------------------------
    can_auto_approve_docx = (
        payment_type == "docx"
        and not is_ai_generated
        and (is_valid_receipt or not admin_targets or conf >= 0.40)
    )
    can_auto_approve_quota = (
        payment_type == "cv_quota"
        and is_valid_receipt
        and not is_ai_generated
        and conf >= 0.70
    )

    if can_auto_approve_docx:
        # اعتماد فوري لنسخة Word
        await db.update_payment_status(
            payment_id,
            "approved",
            admin_note=f"موافقة تلقائية بالذكاء الاصطناعي لنسخة Word (ثقة {conf:.2f})",
        )
        await enqueue(
            settings.queue_docx_generation,
            {
                "payment_id": payment_id,
                "user_id": user_id,
                "cv_record_id": target_cv_id,
            },
        )
        try:
            await bot.send_message(
                chat_id,
                "✅ <b>تم التحقق من إيصال الدفع بنجاح واعتماده تلقائياً بواسطة الذكاء الاصطناعي!</b>\n\n"
                "⏳ جاري تصميم وتجهيز ملف Word القابل للتعديل وإرساله لك فوراً...",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("فشل إرسال إشعار الاعتماد التلقائي للمستخدم %s", chat_id)

        auto_caption = (
            f"⚡️ <b>دفعة معتمدة تلقائياً بالـ AI #{payment_id}</b> ({file_type_badge})\n"
            f"👤 المستخدم: <code>{user_id}</code>\n"
            f"💼 الخدمة: <b>نسخة Word (DOCX)</b>\n"
            f"💵 المبلغ المطلوب: {payment['amount']} {payment['currency']}\n"
            f"🔢 رقم المعاملة بالإيصال: <code>{safe_tx_id}</code>\n"
            f"💰 المبلغ المرصود: {safe_amt}\n"
            f"🎯 المستفيد: {safe_recipient}\n"
            f"🤖 تقييم الذكاء الاصطناعي: {safe_reason} (ثقة {conf:.2f})\n"
            "🟢 <i>تم اعتماد الطلب وتوليد ملف Word تلقائياً.</i>"
        )
        for target_chat_id in admin_targets:
            try:
                if is_pdf:
                    await bot.send_document(chat_id=target_chat_id, document=receipt_file_id, caption=auto_caption, parse_mode="HTML")
                else:
                    try:
                        await bot.send_photo(chat_id=target_chat_id, photo=receipt_file_id, caption=auto_caption, parse_mode="HTML")
                    except Exception:
                        await bot.send_document(chat_id=target_chat_id, document=receipt_file_id, caption=auto_caption, parse_mode="HTML")
            except Exception as exc:
                logger.warning("تعذر تسليم إشعار الدفعة التلقائية إلى الوجهة (%s): %s", target_chat_id, exc)
        return

    elif can_auto_approve_quota:
        # اعتماد فوري لباقة السير الإضافية
        await db.update_payment_status(
            payment_id,
            "approved",
            admin_note=f"موافقة تلقائية بالذكاء الاصطناعي على باقة السير (ثقة {conf:.2f})",
        )
        await db.add_extra_quota(user_id, count=settings.cv_package_size)
        try:
            await bot.send_message(
                chat_id,
                f"🎉 <b>تم التحقق من إيصال الدفع بنجاح واعتماده تلقائياً!</b>\n\n"
                f"تمت إضافة <b>{settings.cv_package_size} سير ذاتية إضافية</b> إلى رصيدك بنجاح! يمكنك الآن بناء سيرتك الذاتية في أي وقت.",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("فشل إرسال إشعار باقة السير للمستخدم %s", chat_id)

        auto_caption = (
            f"⚡️ <b>دفعة معتمدة تلقائياً بالـ AI #{payment_id}</b> ({file_type_badge})\n"
            f"👤 المستخدم: <code>{user_id}</code>\n"
            f"💼 الخدمة: <b>باقة 10 سير ذاتية إضافية</b>\n"
            f"💵 المبلغ: {payment['amount']} {payment['currency']}\n"
            f"🔢 رقم المعاملة بالإيصال: <code>{safe_tx_id}</code>\n"
            f"💰 المبلغ المرصود: {safe_amt}\n"
            f"🎯 المستفيد: {safe_recipient}\n"
            f"🤖 تقييم الذكاء الاصطناعي: {safe_reason} (ثقة {conf:.2f})\n"
            "🟢 <i>تمت إضافة الرصيد لحساب العميل تلقائياً.</i>"
        )
        for target_chat_id in admin_targets:
            try:
                if is_pdf:
                    await bot.send_document(chat_id=target_chat_id, document=receipt_file_id, caption=auto_caption, parse_mode="HTML")
                else:
                    try:
                        await bot.send_photo(chat_id=target_chat_id, photo=receipt_file_id, caption=auto_caption, parse_mode="HTML")
                    except Exception:
                        await bot.send_document(chat_id=target_chat_id, document=receipt_file_id, caption=auto_caption, parse_mode="HTML")
            except Exception as exc:
                logger.warning("تعذر تسليم إشعار الدفعة التلقائية إلى الوجهة (%s): %s", target_chat_id, exc)
        return

    # ------------------------------------------------------------------
    # 7. المسار اليدوي: في حال انخفاض الثقة أو الحاجة لتدقيق بشري
    # ------------------------------------------------------------------
    service_label = "باقة 10 سير ذاتية إضافية" if payment_type == "cv_quota" else "نسخة Word (DOCX)"
    caption = (
        f"🧾 <b>طلب دفع جديد #{payment_id}</b> ({file_type_badge})\n"
        f"👤 معرّف المستخدم: <code>{user_id}</code>\n"
        f"💼 الخدمة المطلوبة: <b>{service_label}</b>\n"
        f"💵 المبلغ المطلوب: {payment['amount']} {payment['currency']}\n"
        f"🔢 رقم المعاملة بالإيصال: <code>{safe_tx_id}</code>\n"
        f"💰 المبلغ المرصود: {safe_amt}\n"
        f"🎯 المستفيد: {safe_recipient}\n"
        f"🤖 تقييم الفحص: {safe_reason} (ثقة {conf:.2f})"
    )

    sent_to_admin = False
    for target_chat_id in admin_targets:
        try:
            if is_pdf:
                await bot.send_document(
                    chat_id=target_chat_id,
                    document=receipt_file_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=admin_review_keyboard(payment_id),
                )
            else:
                try:
                    await bot.send_photo(
                        chat_id=target_chat_id,
                        photo=receipt_file_id,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=admin_review_keyboard(payment_id),
                    )
                except Exception:
                    await bot.send_document(
                        chat_id=target_chat_id,
                        document=receipt_file_id,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=admin_review_keyboard(payment_id),
                    )
            sent_to_admin = True
        except Exception as exc:
            logger.warning("تعذر تسليم إشعار الدفعة إلى الوجهة (%s): %s", target_chat_id, exc)

    if not sent_to_admin:
        logger.error(
            "⚠️ تعذر تسليم إشعار الدفعة لأي مشرف! يرجى التأكد من ضبط ADMIN_GROUP_ID أو ADMIN_USER_IDS وبدء المحادثة مع البوت."
        )

    # 8. إخطار المستخدم بالتحويل للمراجعة اليدوية
    try:
        user_wait_msg = (
            f"🔍 <b>تم استلام إيصالك وتدقيقه بنجاح (طلب #{payment_id}).</b>\n\n"
            "تم تحويل الإيصال إلى فريق الإدارة للمراجعة والاعتماد.\n"
            + ("⏳ سنُرسل لك ملف Word فور الاعتماد." if payment_type == "docx" else "⏳ سنقوم بتفعيل رصيدك فور الاعتماد.")
        )
        await bot.send_message(chat_id, user_wait_msg, parse_mode="HTML")
    except Exception:
        logger.exception("فشل إرسال رسالة التأكيد للمستخدم %s", chat_id)


async def _process_docx_generation(bot: Bot, payload: dict[str, Any]) -> None:
    user_id = payload["user_id"]
    cv_record_id = payload.get("cv_record_id")

    if cv_record_id is None:
        latest = await db.get_latest_cv_record(user_id)
        if latest:
            cv_record = latest
            cv_record_id = latest["id"]
        else:
            logger.error("لا يوجد أي سيرة ذاتية للمستخدم %s مرتبطة بالدفعة %s", user_id, payload.get("payment_id"))
            await bot.send_message(user_id, "⚠️ لم يتم العثور على سيرة ذاتية لتوليد ملف Word. يرجى إنشاء سيرة أولاً.")
            return
    else:
        cv_record = await db.get_cv_record(cv_record_id)
        if cv_record is None:
            latest = await db.get_latest_cv_record(user_id)
            if latest:
                cv_record = latest
            else:
                logger.error("لم يتم العثور على سجل السيرة الذاتية %s", cv_record_id)
                await bot.send_message(user_id, "⚠️ لم يتم العثور على سيرة ذاتية لتوليد ملف Word. يرجى التواصل مع الدعم.")
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
