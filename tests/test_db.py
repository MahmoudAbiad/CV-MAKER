"""
اختبارات الوحدة والتكامل لطبقة قاعدة البيانات (Turso / libSQL / SQLite)
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from typing import Any, AsyncGenerator

import libsql_client
import pytest

from app.services.db import Database, db


TEST_DB_PATH = Path(__file__).resolve().parent / "test_turso.db"
INIT_SQL_PATH = Path(__file__).resolve().parent.parent / "sql" / "init.sql"


@pytest.fixture(scope="module")
def initialized_sqlite_db():
    """تهيئة قاعدة بيانات SQLite اختبارية بالاعتماد على init.sql الحقيقي"""
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()

    # استخدام sqlite3 لتنفيذ ملف الـ SQL بالكامل (بما يشمل التريجرز)
    conn = sqlite3.connect(str(TEST_DB_PATH))
    with open(INIT_SQL_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()

    yield str(TEST_DB_PATH)

    if TEST_DB_PATH.exists():
        try:
            TEST_DB_PATH.unlink()
        except Exception:
            pass


@pytest.fixture
def test_db(initialized_sqlite_db: str) -> Database:
    """كائن Database متصل بملف الاختبار المحلي عبر بروتوكول file:"""
    test_database = Database()
    test_database._client = libsql_client.create_client(f"file:{initialized_sqlite_db}")
    return test_database


@pytest.mark.asyncio
async def test_upsert_user_and_coalesce_preservation(test_db: Database):
    """التحقق من إدراج المستخدم وعدم مسح بياناته عند ورود قيم فارغة (COALESCE)"""
    user_id = 100101

    # 1. إدراج أولي
    await test_db.upsert_user(user_id, "ahmed_dev", "أحمد علي")
    rs = await test_db.client.execute("SELECT username, full_name FROM users WHERE user_id = ?", [user_id])
    assert len(rs.rows) == 1
    assert rs.rows[0][0] == "ahmed_dev"
    assert rs.rows[0][1] == "أحمد علي"

    # 2. تحديث ببيانات جزئية (username = None)
    await test_db.upsert_user(user_id, None, "أحمد علي المحدث")
    rs2 = await test_db.client.execute("SELECT username, full_name FROM users WHERE user_id = ?", [user_id])
    # يجب أن يبقى username محفوظاً بفضل COALESCE
    assert rs2.rows[0][0] == "ahmed_dev"
    assert rs2.rows[0][1] == "أحمد علي المحدث"


@pytest.mark.asyncio
async def test_ensure_user_exists(test_db: Database):
    """التحقق من أن دالة ensure_user_exists تضمن وجود السجل بأمان دون أخطاء"""
    user_id = 100102
    await test_db.ensure_user_exists(user_id)
    # استدعاء متكرر (Idempotent)
    await test_db.ensure_user_exists(user_id)

    rs = await test_db.client.execute("SELECT user_id FROM users WHERE user_id = ?", [user_id])
    assert len(rs.rows) == 1
    assert rs.rows[0][0] == user_id


@pytest.mark.asyncio
async def test_insert_and_get_cv_record(test_db: Database, sample_arabic_cv_data: dict[str, Any]):
    """التحقق من إدراج واسترجاع سجل السيرة الذاتية بصيغة JSON وفحص الصيغة"""
    user_id = 100103
    await test_db.ensure_user_exists(user_id)

    # 1. إدراج سجل صالح
    cv_id = await test_db.insert_cv_record(user_id, sample_arabic_cv_data, fmt="pdf")
    assert isinstance(cv_id, int)
    assert cv_id > 0

    # 2. استرجاع السجل
    record = await test_db.get_cv_record(cv_id)
    assert record is not None
    assert record["id"] == cv_id
    assert record["user_id"] == user_id
    assert record["format"] == "pdf"
    assert record["parsed_json"]["full_name"] == sample_arabic_cv_data["full_name"]

    # 3. صيغة غير صالحة يجب أن تثير ValueError
    with pytest.raises(ValueError, match="صيغة غير مدعومة"):
        await test_db.insert_cv_record(user_id, sample_arabic_cv_data, fmt="exe")


@pytest.mark.asyncio
async def test_payments_lifecycle_and_status_update(test_db: Database):
    """التحقق من دورة حياة الدفعة (إدراج -> قراءة -> تحديث الحالة -> الملاحظات)"""
    user_id = 100104
    await test_db.ensure_user_exists(user_id)

    # 1. إدراج الدفعة بحالة pending
    payment_id = await test_db.insert_payment(
        user_id=user_id,
        amount=0.20,
        currency="USD",
        receipt_file_id="tg_photo_file_id_test_001",
        cv_record_id=None,
    )
    assert isinstance(payment_id, int)
    assert payment_id > 0

    # 2. استرجاع الدفعة
    payment = await test_db.get_payment(payment_id)
    assert payment is not None
    assert payment["id"] == payment_id
    assert payment["status"] == "pending"
    assert payment["amount"] == 0.20
    assert payment["receipt_file_id"] == "tg_photo_file_id_test_001"

    # 3. التحقق من وجود دفعة معلقة للمستخدم
    has_pending = await test_db.has_pending_payment(user_id)
    assert has_pending is True

    # 4. تحديث حالة الدفعة إلى approved
    updated = await test_db.update_payment_status(payment_id, "approved", admin_note="تم التحقق من الحوالة")
    assert updated is True

    payment_after = await test_db.get_payment(payment_id)
    assert payment_after["status"] == "approved"
    assert payment_after["admin_note"] == "تم التحقق من الحوالة"

    # الآن لم تعد معلقة
    assert await test_db.has_pending_payment(user_id) is False

    # 5. حالة غير صالحة يجب أن تثير خطأ
    with pytest.raises(ValueError, match="حالة دفعة غير صالحة"):
        await test_db.update_payment_status(payment_id, "unknown_status")


@pytest.mark.asyncio
async def test_anti_fraud_receipt_and_transaction_deduplication(test_db: Database):
    """
    التحقق من آليات مكافحة الاحتيال والإنفاق المزدوج:
    - كشف تكرار صورة الإيصال (is_receipt_reused)
    - كشف تكرار رقم المعاملة المالي (is_transaction_id_reused)
    """
    user_id = 100105
    await test_db.ensure_user_exists(user_id)

    file_id = "unique_receipt_file_123"
    tx_code = "TX-SHAM-2026-999"

    # إدراج دفعة أولى
    pid1 = await test_db.insert_payment(
        user_id=user_id,
        amount=0.20,
        currency="USD",
        receipt_file_id=file_id,
        cv_record_id=None,
    )
    await test_db.update_payment_transaction_id(pid1, tx_code)

    # فحص تكرار صورة الإيصال
    # تكرار مع استثناء نفس الدفعة -> False
    assert await test_db.is_receipt_reused(file_id, exclude_payment_id=pid1) is False
    # تكرار بدون استثناء -> True
    assert await test_db.is_receipt_reused(file_id) is True

    # فحص تكرار رقم العملية المالي (Anti Double-Spending)
    assert await test_db.is_transaction_id_reused(tx_code, exclude_payment_id=pid1) is False
    assert await test_db.is_transaction_id_reused(tx_code) is True

    # رقم عملية غير موجود أو رمز شرطة
    assert await test_db.is_transaction_id_reused("-") is False
    assert await test_db.is_transaction_id_reused("NON_EXISTENT_TX") is False


@pytest.mark.asyncio
async def test_get_stats_atomic_aggregation(test_db: Database):
    """التحقق من دقة استعلام الإحصائيات الموحد السريع get_stats"""
    stats = await test_db.get_stats()
    assert isinstance(stats, dict)
    assert "total_users" in stats
    assert "pdf_generated" in stats
    assert "docx_generated" in stats
    assert "payments_pending" in stats
    assert "payments_approved" in stats
    assert "payments_rejected" in stats

    assert stats["total_users"] >= 4
    assert stats["payments_approved"] >= 1


@pytest.mark.asyncio
async def test_get_all_user_ids(test_db: Database):
    """التحقق من جلب قائمة معرّفات جميع المستخدمين للبث الجماعي"""
    user_ids = await test_db.get_all_user_ids()
    assert isinstance(user_ids, list)
    assert 100101 in user_ids
    assert 100102 in user_ids
