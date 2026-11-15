"""
اختبارات الوحدة والتكامل لعمّال الخلفية وطوابير المهام (queue/worker)
"""
from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.queue.worker import (
    _HANDLERS,
    _process_cv_generation,
    _process_docx_generation,
    _process_receipt_screening,
)
from app.services.db import db


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()
    bot.send_document = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.get_file = AsyncMock()
    bot.download_file = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_process_cv_generation_success(
    mock_bot: AsyncMock, sample_arabic_cv_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """التحقق من دورة عمل عامل توليد الـ PDF وحفظ السجل وإرسال العرض"""
    chat_id = 987654
    user_id = 987654
    payload = {
        "chat_id": chat_id,
        "user_id": user_id,
        "cv_data": sample_arabic_cv_data,
    }

    # محاكاة حفظ قاعدة البيانات
    mock_insert_cv = AsyncMock(return_value=42)
    monkeypatch.setattr(db, "insert_cv_record", mock_insert_cv)

    await _process_cv_generation(mock_bot, payload)

    # التحقق من استدعاء إرسال الوثيقة
    mock_bot.send_document.assert_called_once()
    doc_call_kwargs = mock_bot.send_document.call_args[1]
    assert doc_call_kwargs["chat_id"] == chat_id
    assert "PDF" in doc_call_kwargs["caption"]

    # التحقق من حفظ السجل بالمعرف 42
    mock_insert_cv.assert_called_once_with(user_id=user_id, parsed_json=sample_arabic_cv_data, fmt="pdf")

    # التحقق من إرسال عرض نسخة Word
    mock_bot.send_message.assert_called_once()
    assert "Word" in mock_bot.send_message.call_args[0][1]


@pytest.mark.asyncio
async def test_process_docx_generation_success(
    mock_bot: AsyncMock, sample_arabic_cv_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    """التحقق من دورة عمل عامل توليد الـ DOCX واسترجاع البيانات وإرسال الملف"""
    user_id = 987654
    cv_record_id = 15
    payload = {
        "payment_id": 101,
        "user_id": user_id,
        "cv_record_id": cv_record_id,
    }

    mock_get_cv = AsyncMock(
        return_value={
            "id": cv_record_id,
            "user_id": user_id,
            "parsed_json": sample_arabic_cv_data,
            "format": "pdf",
            "created_at": "2026-11-15 00:00:00",
        }
    )
    mock_insert_cv = AsyncMock(return_value=16)
    monkeypatch.setattr(db, "get_cv_record", mock_get_cv)
    monkeypatch.setattr(db, "insert_cv_record", mock_insert_cv)

    await _process_docx_generation(mock_bot, payload)

    # التحقق من إرسال مستند Word
    mock_bot.send_document.assert_called_once()
    doc_call_kwargs = mock_bot.send_document.call_args[1]
    assert doc_call_kwargs["chat_id"] == user_id
    assert "Word" in doc_call_kwargs["caption"]


@pytest.mark.asyncio
async def test_process_receipt_screening_invalid_image_auto_reject(
    mock_bot: AsyncMock, monkeypatch: pytest.MonkeyPatch
):
    """التحقق من الرفض التلقائي عندما تكشف Gemini Vision أن الصورة ليست إيصال دفع"""
    payment_id = 55
    chat_id = 987654
    payload = {
        "payment_id": payment_id,
        "chat_id": chat_id,
        "user_id": chat_id,
        "receipt_file_id": "dummy_photo_123",
    }

    # محاكاة تحميل الصورة (صورة JPEG بسيطة صالحة)
    jpeg_dummy_bytes = b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xFF\xDB\x00C\x00\xFF\xD9"
    mock_file = MagicMock()
    mock_file.file_size = 1000
    mock_file.file_path = "photos/photo.jpg"
    mock_bot.get_file = AsyncMock(return_value=mock_file)
    mock_bot.download_file = AsyncMock(return_value=io.BytesIO(jpeg_dummy_bytes))

    # محاكاة فحص Gemini: ليست إيصال دفع بثقة 0.85
    mock_verdict = {
        "is_receipt": False,
        "confidence": 0.85,
        "reason": "صورة شخصية وليست إيصال تحويل مالي",
    }
    monkeypatch.setattr("app.services.gemini_service.verify_payment_receipt", AsyncMock(return_value=mock_verdict))

    mock_update_status = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "update_payment_status", mock_update_status)

    await _process_receipt_screening(mock_bot, payload)

    # التحقق من تسجيل الرفض
    mock_update_status.assert_called_once_with(
        payment_id, "rejected", admin_note="الصورة ليست إيصال دفع صالح"
    )
    # التحقق من إخطار المستخدم
    mock_bot.send_message.assert_called_once()
    assert "لا تبدو كإيصال دفع صالح" in mock_bot.send_message.call_args[0][1]


@pytest.mark.asyncio
async def test_process_receipt_screening_pdf_success(
    mock_bot: AsyncMock, monkeypatch: pytest.MonkeyPatch
):
    """التحقق من دعم فحص وتدقيق إيصالات الدفع المرسلة كملف PDF"""
    payment_id = 77
    chat_id = 987654
    payload = {
        "payment_id": payment_id,
        "chat_id": chat_id,
        "user_id": chat_id,
        "receipt_file_id": "dummy_pdf_doc_123",
        "is_pdf": True,
    }

    # بايتات PDF صالحة تبدأ بـ %PDF
    pdf_dummy_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    mock_file = MagicMock()
    mock_file.file_size = 500
    mock_file.file_path = "documents/receipt.pdf"
    mock_bot.get_file = AsyncMock(return_value=mock_file)
    mock_bot.download_file = AsyncMock(return_value=io.BytesIO(pdf_dummy_bytes))

    mock_verdict = {
        "is_receipt": True,
        "confidence": 0.95,
        "transaction_id": "TX998877",
        "amount": "25000",
        "recipient": "123456",
        "reason": "إيصال شام كاش صالح ومطابق",
    }
    mock_verify = AsyncMock(return_value=mock_verdict)
    monkeypatch.setattr("app.services.gemini_service.verify_payment_receipt", mock_verify)

    mock_payment = {"amount": 0.20, "currency": "USD", "user_id": chat_id, "cv_record_id": 10, "payment_type": "docx"}
    monkeypatch.setattr(db, "get_payment", AsyncMock(return_value=mock_payment))
    monkeypatch.setattr(db, "is_receipt_reused", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "is_transaction_id_reused", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "update_payment_transaction_id", AsyncMock(return_value=True))
    mock_update_status = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "update_payment_status", mock_update_status)
    mock_enqueue = AsyncMock(return_value="job_docx_123")
    monkeypatch.setattr("app.queue.worker.enqueue", mock_enqueue)

    await _process_receipt_screening(mock_bot, payload)

    # التحقق من تمرير application/pdf لـ Gemini
    mock_verify.assert_called_once_with(pdf_dummy_bytes, mime_type="application/pdf")
    # التحقق من الاعتماد التلقائي بواسطة الذكاء الاصطناعي وإدراج توليد Word بالطابور
    mock_update_status.assert_called_once()
    assert mock_update_status.call_args[0][1] == "approved"
    mock_enqueue.assert_called_once()
    # التحقق من إرسال مستند PDF (send_document) للأدمن كإشعار اعتماد آلي
    mock_bot.send_document.assert_called()
    # التحقق من إرسال رسالة التأكيد والاعتماد للمستخدم
    mock_bot.send_message.assert_called()


def test_handlers_map_registry():
    """التحقق من تسجيل العمال الثلاثة في القاموس المعياري لطوابير Redis"""
    assert "queue:cv_generation" in _HANDLERS
    assert "queue:receipt_screening" in _HANDLERS
    assert "queue:docx_generation" in _HANDLERS
    assert callable(_HANDLERS["queue:cv_generation"])
    assert callable(_HANDLERS["queue:receipt_screening"])
    assert callable(_HANDLERS["queue:docx_generation"])


@pytest.mark.asyncio
async def test_dequeue_multi_blocking_backoff_on_connection_error(monkeypatch: pytest.MonkeyPatch):
    """التحقق من تفادي حلقة التكرار السريعة (Busy Loop) بانتظار مؤقت عند انقطاع Redis"""
    from redis.exceptions import ConnectionError as RedisConnError
    from app.queue.redis_client import dequeue_multi_blocking

    mock_redis = MagicMock()
    mock_redis.brpop = AsyncMock(side_effect=RedisConnError("Error -2 connecting to host: Name or service not known"))

    sleep_called_with = []
    async def fake_sleep(secs: float):
        sleep_called_with.append(secs)

    monkeypatch.setattr("app.queue.redis_client.get_redis", lambda: mock_redis)
    monkeypatch.setattr("app.queue.redis_client.reset_redis", AsyncMock())
    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    result = await dequeue_multi_blocking(["test_queue"], timeout=30)
    assert result is None
    assert 2.0 in sleep_called_with


@pytest.mark.asyncio
async def test_receipt_screening_fake_ai_generated_goes_to_manual_review(
    mock_bot: AsyncMock, monkeypatch: pytest.MonkeyPatch
):
    """التحقق من أن الإيصال المصطنع بالـ AI لا يُعتمد آلياً بل يحال للمراجعة اليدوية"""
    payment_id = 88
    chat_id = 998877
    payload = {
        "payment_id": payment_id,
        "chat_id": chat_id,
        "user_id": chat_id,
        "receipt_file_id": "fake_ai_receipt_id",
        "is_pdf": False,
    }

    dummy_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 30
    mock_file = MagicMock()
    mock_file.file_size = 500
    mock_file.file_path = "photos/receipt.jpg"
    mock_bot.get_file = AsyncMock(return_value=mock_file)
    mock_bot.download_file = AsyncMock(return_value=io.BytesIO(dummy_jpeg))

    # نتيجة الفحص تكشف أنه مولد بالذكاء الاصطناعي (is_ai_generated: True)
    mock_verdict = {
        "is_receipt": True,
        "is_ai_generated": True,
        "confidence": 0.85,
        "transaction_id": "TX_AI_FAKE",
        "amount": "25000",
        "recipient": "123456",
        "reason": "صورة مصطنعة ومولدة عبر الذكاء الاصطناعي",
    }
    monkeypatch.setattr("app.services.gemini_service.verify_payment_receipt", AsyncMock(return_value=mock_verdict))

    mock_payment = {"amount": 0.20, "currency": "USD", "user_id": chat_id, "cv_record_id": 11, "payment_type": "docx"}
    monkeypatch.setattr(db, "get_payment", AsyncMock(return_value=mock_payment))
    monkeypatch.setattr(db, "is_receipt_reused", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "is_transaction_id_reused", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "update_payment_transaction_id", AsyncMock(return_value=True))

    mock_update_status = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "update_payment_status", mock_update_status)
    mock_enqueue = AsyncMock()
    monkeypatch.setattr("app.queue.worker.enqueue", mock_enqueue)

    await _process_receipt_screening(mock_bot, payload)

    # يجب ألا تتم الموافقة التلقائية، ويجب ألا يُدرج في طابور docx
    mock_update_status.assert_not_called()
    mock_enqueue.assert_not_called()
    # يجب إرسال الإشعار للأدمن مع أزرار المراجعة والرفض اليدوي
    mock_bot.send_photo.assert_called()


@pytest.mark.asyncio
async def test_cv_generation_blocks_when_quota_exhausted(
    mock_bot: AsyncMock, monkeypatch: pytest.MonkeyPatch
):
    """التحقق من أن عامل توليد السيرة يرفض العملية عند استنفاد كوتة المستخدم"""
    user_id = 554433
    payload = {
        "chat_id": user_id,
        "user_id": user_id,
        "cv_data": {"full_name": "Test User"},
    }

    # محاكاة كوتة مستنفدة (0 متبقي)
    mock_quota = {"total_available": 0, "monthly_used": 10, "remaining_monthly": 0, "extra_balance": 0}
    monkeypatch.setattr(db, "get_user_quota", AsyncMock(return_value=mock_quota))
    monkeypatch.setattr(db, "get_cv_quota_price_syp", AsyncMock(return_value="250"))
    mock_consume = AsyncMock()
    monkeypatch.setattr(db, "consume_user_quota", mock_consume)

    await _process_cv_generation(mock_bot, payload)

    # يجب عدم خصم أي رصيد
    mock_consume.assert_not_called()
    # إرسال رسالة نفاد الرصيد للمستخدم
    mock_bot.send_message.assert_called()
    sent_text = mock_bot.send_message.call_args[0][1]
    assert "استنفدت رصيدك" in sent_text


@pytest.mark.asyncio
async def test_process_receipt_screening_docx_auto_approval_resilience(
    mock_bot: AsyncMock, monkeypatch: pytest.MonkeyPatch
):
    """التحقق من الاعتماد التلقائي لنسخة Word ومرونة استرجاع آخر سيرة ذاتية والتعامل مع أخطاء إرسال الصور"""
    payment_id = 99
    user_id = 112233
    payload = {
        "payment_id": payment_id,
        "chat_id": user_id,
        "user_id": user_id,
        "receipt_file_id": "receipt_doc_file_id",
        "is_pdf": False,
    }

    dummy_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 30
    mock_file = MagicMock()
    mock_file.file_size = 500
    mock_file.file_path = "photos/receipt.jpg"
    mock_bot.get_file = AsyncMock(return_value=mock_file)
    mock_bot.download_file = AsyncMock(return_value=io.BytesIO(dummy_jpeg))

    # محاكاة إخفاق send_photo (كأن التيليجرام يعتبره مستنداً وليس صورة) ثم نجاح send_document
    mock_bot.send_photo = AsyncMock(side_effect=Exception("Bad Request: file is not a photo"))
    mock_bot.send_document = AsyncMock()

    mock_verdict = {
        "is_receipt": True,
        "is_ai_generated": False,
        "confidence": 0.55,  # نسبة معتدلة تكفي لنسخة Word
        "transaction_id": "TX556677",
        "amount": "25",
        "recipient": "999",
        "reason": "إيصال سليم وحقيقي",
    }
    monkeypatch.setattr("app.services.gemini_service.verify_payment_receipt", AsyncMock(return_value=mock_verdict))

    # دفعة بدون cv_record_id مرتبط
    mock_payment = {"amount": 0.20, "currency": "USD", "user_id": user_id, "cv_record_id": None, "payment_type": "docx"}
    monkeypatch.setattr(db, "get_payment", AsyncMock(return_value=mock_payment))
    monkeypatch.setattr(db, "get_latest_cv_record", AsyncMock(return_value={"id": 777, "parsed_json": {"full_name": "Test"}}))
    monkeypatch.setattr(db, "is_receipt_reused", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "is_transaction_id_reused", AsyncMock(return_value=False))
    monkeypatch.setattr(db, "update_payment_transaction_id", AsyncMock(return_value=True))
    mock_update_status = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "update_payment_status", mock_update_status)
    mock_enqueue = AsyncMock(return_value="job_docx_99")
    monkeypatch.setattr("app.queue.worker.enqueue", mock_enqueue)

    await _process_receipt_screening(mock_bot, payload)

    # التحقق من الموافقة التلقائية لنسخة Word
    mock_update_status.assert_called_once()
    assert mock_update_status.call_args[0][1] == "approved"
    # التحقق من إدراج المهمة بالطابور بالمعرف 777 المسترجع تلقائياً
    mock_enqueue.assert_called_once()
    assert mock_enqueue.call_args[0][1]["cv_record_id"] == 777
    # التحقق من استخدام send_document كبديل عند إخفاق send_photo
    mock_bot.send_document.assert_called()
    mock_bot.send_message.assert_called()

