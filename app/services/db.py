"""
طبقة الوصول لقاعدة بيانات Turso (libSQL) بشكل غير متزامن ومحصّن بالكامل.
يوفر:
- معالجة آمنة ومصححة لـ Monkey Patching مع دعم كافة صيغ أخطاء libSQL/HTTP.
- دمج الاستعلامات وتقليل رحلات الشبكة (Network Round-trips).
- فحص اتصال حقيقي (Health Check / Ping) عند الإقلاع.
- أمان فائق ضد حقن SQL وحماية من مسح البيانات عبر COALESCE.
- معالجة قوية للاستثناءات والتحقق من صحة المدخلات.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import libsql_client
from libsql_client.client import LibsqlError
from libsql_client.http import HttpClient

from app.config import settings

logger = logging.getLogger(__name__)


def _now() -> str:
    """إرجاع التوقيت الحالي بتنسيق UTC القياسي."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------
# معالجة محصنة لخلل استجابة الخطأ في libsql-client==0.3.1
# عندما يرجع Turso كود 200 لكن الجسم يحتوي على "error" بدل "result".
# ----------------------------------------------------------------------
if not getattr(HttpClient, "_is_error_patched", False):
    _original_send = HttpClient._send

    async def _safe_patched_send(self: HttpClient, method: str, path: str, request_body: Any) -> Any:
        response = await _original_send(self, method, path, request_body)
        if isinstance(response, dict) and "result" not in response and "error" in response:
            raw_err = response["error"]
            if isinstance(raw_err, dict):
                message = raw_err.get("message", "خطأ من قاعدة البيانات (استجابة بدون result)")
                code = raw_err.get("code", "UNKNOWN")
            elif isinstance(raw_err, str):
                message = raw_err
                code = "SERVER_ERROR"
            else:
                message = "استجابة خطأ غير متوقعة من خادم قاعدة البيانات"
                code = "UNKNOWN"
            raise LibsqlError(message, code)
        return response

    HttpClient._send = _safe_patched_send  # type: ignore[method-assign]
    HttpClient._is_error_patched = True    # type: ignore[attr-defined]


class Database:
    """غلاف متقدم حول عميل libsql-client غير المتزامن لإدارة العمليات بأمان وكفاءة."""

    VALID_PAYMENT_STATUSES = {"pending", "approved", "rejected"}
    VALID_CV_FORMATS = {"pdf", "docx"}

    def __init__(self) -> None:
        self._client: Optional[libsql_client.Client] = None

    async def connect(self) -> None:
        """تهيئة العميل والتحقق الفعلي من صحة الاتصال وصلاحية التوكن."""
        if self._client is None:
            self._client = libsql_client.create_client(
                url=settings.turso_db_url,
                auth_token=settings.turso_auth_token,
            )
            # فحص حيوي (Ping) للتأكد من وصول الشبكة وصلاحية الاعتماد
            try:
                await self._client.execute("SELECT 1")
                logger.info("✅ تم التحقق من سلامة الاتصال بقاعدة بيانات Turso بنجاح")
            except Exception as e:
                logger.error("❌ فشل اختبار الاتصال الفعلي بقاعدة بيانات Turso: %s", e)
                await self.close()
                raise ConnectionError(f"تعذر الاتصال بقاعدة بيانات Turso: {e}") from e

            # ترقية المخطط تلقائياً لضمان توفر جداول وحقول التحديثات الجديدة
            try:
                await self._run_migrations()
            except Exception as e:
                logger.warning("تنبيه ترحيل مخطط قاعدة البيانات: %s", e)

    async def _run_migrations(self) -> None:
        """ترقية المخطط تلقائياً لضمان وجود الجداول والحقول المضافة حديثاً."""
        migration_statements = [
            """
            CREATE TABLE IF NOT EXISTS user_quotas (
                user_id             INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                monthly_used        INTEGER NOT NULL DEFAULT 0,
                extra_balance       INTEGER NOT NULL DEFAULT 0,
                reset_month         TEXT NOT NULL,
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_user_quotas_reset_month ON user_quotas(reset_month)",
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key                 TEXT PRIMARY KEY,
                value               TEXT NOT NULL,
                updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
        ]
        for stmt in migration_statements:
            try:
                await self.client.execute(stmt)
            except Exception as e:
                logger.warning("تنبيه ترحيل المخطط: %s", e)

        # إضافة حقل payment_type إلى جدول payments إذا لم يكن موجوداً
        try:
            await self.client.execute("ALTER TABLE payments ADD COLUMN payment_type TEXT DEFAULT 'docx'")
        except Exception:
            pass

    async def close(self) -> None:
        """إغلاق العميل وتحرير الموارد."""
        if self._client is not None:
            await self._client.close()
            self._client = None
            logger.info("تم إغلاق اتصال قاعدة البيانات.")

    @property
    def client(self) -> libsql_client.Client:
        if self._client is None:
            raise RuntimeError("قاعدة البيانات غير متصلة بعد. استدعِ connect() أولاً.")
        return self._client

    # ------------------------------------------------------------------
    # المستخدمون
    # ------------------------------------------------------------------
    async def upsert_user(self, user_id: int, username: str | None, full_name: str | None) -> None:
        """
        إدراج أو تحديث المستخدم.
        يستخدم COALESCE لمنع مسح بيانات الـ username أو full_name إذا وردت فارغة.
        """
        await self.client.execute(
            """
            INSERT INTO users (user_id, username, full_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, users.username),
                full_name = COALESCE(excluded.full_name, users.full_name)
            """,
            [user_id, username, full_name, _now()],
        )

    async def ensure_user_exists(self, user_id: int) -> None:
        """يضمن وجود سجل أساسي للمستخدم لتفادي أخطاء المفاتيح الأجنبية."""
        await self.client.execute(
            """
            INSERT INTO users (user_id, created_at)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            [user_id, _now()],
        )

    # ------------------------------------------------------------------
    # السير الذاتية
    # ------------------------------------------------------------------
    async def insert_cv_record(self, user_id: int, parsed_json: dict[str, Any], fmt: str) -> int:
        """حفظ سجل السيرة الذاتية مع التحقق من الصيغة وضمان وجود المستخدم."""
        if fmt not in self.VALID_CV_FORMATS:
            raise ValueError(f"صيغة غير مدعومة: {fmt}. الصيغ المسموحة: {self.VALID_CV_FORMATS}")

        json_str = json.dumps(parsed_json, ensure_ascii=False)
        now = _now()

        try:
            rs = await self.client.execute(
                """
                INSERT INTO cv_records (user_id, parsed_json, format, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [user_id, json_str, fmt, now],
            )
        except LibsqlError as e:
            if "FOREIGN KEY" in str(e).upper():
                logger.warning("المستخدم %s غير موجود أثناء إدراج CV، جاري إنشاؤه وإعادة المحاولة...", user_id)
                await self.ensure_user_exists(user_id)
                rs = await self.client.execute(
                    """
                    INSERT INTO cv_records (user_id, parsed_json, format, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [user_id, json_str, fmt, now],
                )
            else:
                raise

        if rs.last_insert_rowid is None:
            raise RuntimeError("فشل استرجاع معرف السجل المُدرج (last_insert_rowid is None)")
        return int(rs.last_insert_rowid)

    async def get_cv_record(self, cv_id: int) -> Optional[dict[str, Any]]:
        """استرجاع بيانات سيرة ذاتية بواسطة معرّفها."""
        rs = await self.client.execute(
            "SELECT id, user_id, parsed_json, format, created_at FROM cv_records WHERE id = ?",
            [cv_id],
        )
        if not rs.rows:
            return None
        row = rs.rows[0]
        try:
            parsed = json.loads(row[2])
        except (TypeError, json.JSONDecodeError):
            logger.error("تعذر فك ترميز JSON للسيرة الذاتية رقم %s", cv_id)
            parsed = {}

        return {
            "id": row[0],
            "user_id": row[1],
            "parsed_json": parsed,
            "format": row[3],
            "created_at": row[4],
        }

    async def get_latest_cv_record(self, user_id: int) -> Optional[dict[str, Any]]:
        """استرجاع أحدث سيرة ذاتية للمستخدم لربط الدفعات ونسخ Word تلقائياً."""
        rs = await self.client.execute(
            "SELECT id, user_id, parsed_json, format, created_at FROM cv_records WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            [user_id],
        )
        if not rs.rows:
            return None
        row = rs.rows[0]
        try:
            parsed = json.loads(row[2])
        except (TypeError, json.JSONDecodeError):
            logger.error("تعذر فك ترميز JSON للسيرة الذاتية رقم %s للمستخدم %s", row[0], user_id)
            parsed = {}

        return {
            "id": row[0],
            "user_id": row[1],
            "parsed_json": parsed,
            "format": row[3],
            "created_at": row[4],
        }

    # ------------------------------------------------------------------
    # المدفوعات
    # ------------------------------------------------------------------
    async def insert_payment(
        self,
        user_id: int,
        amount: float,
        currency: str,
        receipt_file_id: str,
        cv_record_id: int | None = None,
        payment_type: str = "docx",
    ) -> int:
        """إدراج دفعة جديدة مع الربط الذكي بالمستخدم والسيرة الذاتية ونوع الدفعة."""
        now = _now()
        sql_with_type = """
            INSERT INTO payments
                (user_id, amount, currency, receipt_file_id, status, created_at, updated_at, cv_record_id, payment_type)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        """
        params_with_type = [user_id, amount, currency, receipt_file_id, now, now, cv_record_id, payment_type]

        try:
            rs = await self.client.execute(sql_with_type, params_with_type)
        except LibsqlError as e:
            err_msg = str(e).upper()
            if "FOREIGN KEY" in err_msg:
                logger.warning("المستخدم %s غير موجود أثناء تسجيل الدفعة، جاري إنشاؤه وإعادة المحاولة...", user_id)
                await self.ensure_user_exists(user_id)
                rs = await self.client.execute(sql_with_type, params_with_type)
            elif "NO COLUMN NAMED PAYMENT_TYPE" in err_msg:
                # توافق عكسي في حال لم تكن الخانة موجودة بعد
                rs = await self.client.execute(
                    """
                    INSERT INTO payments
                        (user_id, amount, currency, receipt_file_id, status, created_at, updated_at, cv_record_id)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    [user_id, amount, currency, receipt_file_id, now, now, cv_record_id],
                )
            else:
                raise

        if rs.last_insert_rowid is None:
            raise RuntimeError("فشل استرجاع معرف الدفعة المُدرجة")
        return int(rs.last_insert_rowid)

    async def get_payment(self, payment_id: int) -> Optional[dict[str, Any]]:
        """استرجاع بيانات دفعة معينة."""
        try:
            rs = await self.client.execute(
                """
                SELECT id, user_id, amount, currency, receipt_file_id, status,
                       admin_note, created_at, updated_at, cv_record_id, transaction_id, payment_type
                FROM payments WHERE id = ?
                """,
                [payment_id],
            )
        except Exception:
            rs = await self.client.execute(
                """
                SELECT id, user_id, amount, currency, receipt_file_id, status,
                       admin_note, created_at, updated_at, cv_record_id, transaction_id
                FROM payments WHERE id = ?
                """,
                [payment_id],
            )

        if not rs.rows:
            return None
        r = rs.rows[0]
        payment_type = "docx"
        if len(r) > 11 and r[11]:
            payment_type = str(r[11])
        elif r[9] is None:
            payment_type = "cv_quota"

        return {
            "id": r[0],
            "user_id": r[1],
            "amount": r[2],
            "currency": r[3],
            "receipt_file_id": r[4],
            "status": r[5],
            "admin_note": r[6],
            "created_at": r[7],
            "updated_at": r[8],
            "cv_record_id": r[9],
            "transaction_id": r[10] if len(r) > 10 else None,
            "payment_type": payment_type,
        }

    async def update_payment_transaction_id(self, payment_id: int, transaction_id: str) -> None:
        """تحديث معرّف العملية المالي المستخرج من الإيصال."""
        await self.client.execute(
            """
            UPDATE payments
            SET transaction_id = ?, updated_at = ?
            WHERE id = ?
            """,
            [transaction_id, _now(), payment_id],
        )

    async def has_pending_payment(self, user_id: int) -> bool:
        """التحقق مما إذا كان لدى المستخدم طلب دفع معلق قيد المراجعة لمنع الإغراق."""
        # نعتبر الطلبات المعلقة لأكثر من ساعتين منتهية الصلاحية لتفادي قفل حساب المستخدم في حال غياب المشرف
        rs = await self.client.execute(
            """
            SELECT id FROM payments 
            WHERE user_id = ? AND status = 'pending' 
              AND datetime(created_at) > datetime('now', '-2 hours')
            LIMIT 1
            """,
            [user_id],
        )
        return len(rs.rows) > 0

    async def cancel_user_pending_payments(self, user_id: int) -> None:
        """إلغاء أي دفعات معلقة للمستخدم عند طلب الإلغاء لتمكينه من إعادة المحاولة فوراً."""
        await self.client.execute(
            "UPDATE payments SET status = 'cancelled', updated_at = ? WHERE user_id = ? AND status = 'pending'",
            [_now(), user_id],
        )

    async def is_receipt_reused(self, receipt_file_id: str, exclude_payment_id: int | None = None) -> bool:
        """
        التحقق من تكرار استخدام صورة الإيصال (file_id) في عمليات سابقة مقبولة أو قيد المعالجة.
        """
        if exclude_payment_id:
            rs = await self.client.execute(
                "SELECT 1 FROM payments WHERE receipt_file_id = ? AND id != ? AND status IN ('approved', 'pending') LIMIT 1",
                [receipt_file_id, exclude_payment_id],
            )
        else:
            rs = await self.client.execute(
                "SELECT 1 FROM payments WHERE receipt_file_id = ? AND status IN ('approved', 'pending') LIMIT 1",
                [receipt_file_id],
            )
        return len(rs.rows) > 0

    async def is_transaction_id_reused(self, transaction_id: str, exclude_payment_id: int | None = None) -> bool:
        """
        التحقق من تكرار استخدام رقم العملية المالي (Anti Double-Spending) في عمليات سابقة.
        """
        if not transaction_id or transaction_id.strip() in ("", "-", "null", "none"):
            return False
        tx = transaction_id.strip()
        if exclude_payment_id:
            rs = await self.client.execute(
                "SELECT 1 FROM payments WHERE transaction_id = ? AND id != ? AND status IN ('approved', 'pending') LIMIT 1",
                [tx, exclude_payment_id],
            )
        else:
            rs = await self.client.execute(
                "SELECT 1 FROM payments WHERE transaction_id = ? AND status IN ('approved', 'pending') LIMIT 1",
                [tx],
            )
        return len(rs.rows) > 0

    async def update_payment_status(
        self, payment_id: int, status: str, admin_note: str | None = None
    ) -> bool:
        """
        تحديث حالة الدفعة مع التحقق من صحة الحالة والاحتفاظ بالملاحظات السابقة إذا لم يتم تمرير ملاحظة جديدة.
        تُرجع True إذا تم تحديث الصف بالفعل، أو False إذا لم يتم العثور على الدفعة.
        """
        if status not in self.VALID_PAYMENT_STATUSES:
            raise ValueError(f"حالة دفعة غير صالحة: {status}. الحالات المتاحة: {self.VALID_PAYMENT_STATUSES}")

        rs = await self.client.execute(
            """
            UPDATE payments
            SET status = ?,
                admin_note = COALESCE(?, admin_note),
                updated_at = ?
            WHERE id = ?
            """,
            [status, admin_note, _now(), payment_id],
        )
        return rs.rows_affected > 0

    # ------------------------------------------------------------------
    # إحصائيات لوحة تحكم الأدمن (استعلام موحد ذري فائق السرعة)
    # ------------------------------------------------------------------
    async def get_stats(self) -> dict[str, Any]:
        """
        استخراج إحصائيات النظام بالكامل في رحلة شبكية واحدة (Single Round-trip).
        يوفر أكثر من 80% من زمن الاستجابة ويضمن اتساق البيانات اللحظي.
        """
        rs = await self.client.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS total_users,
                (SELECT COUNT(*) FROM cv_records WHERE format = 'pdf') AS pdf_generated,
                (SELECT COUNT(*) FROM cv_records WHERE format = 'docx') AS docx_generated,
                (SELECT COUNT(*) FROM payments WHERE status = 'pending') AS payments_pending,
                (SELECT COUNT(*) FROM payments WHERE status = 'approved') AS payments_approved,
                (SELECT COUNT(*) FROM payments WHERE status = 'rejected') AS payments_rejected
            """
        )
        row = rs.rows[0]
        return {
            "total_users": row[0] or 0,
            "pdf_generated": row[1] or 0,
            "docx_generated": row[2] or 0,
            "payments_pending": row[3] or 0,
            "payments_approved": row[4] or 0,
            "payments_rejected": row[5] or 0,
        }

    async def get_all_user_ids(self) -> list[int]:
        """استرجاع كافة معرّفات المستخدمين."""
        rs = await self.client.execute("SELECT user_id FROM users")
        return [row[0] for row in rs.rows]

    # ------------------------------------------------------------------
    # إدارة كوتة السير الذاتية (الحد الشهري 10 والباقات الإضافية)
    # ------------------------------------------------------------------
    @staticmethod
    def _current_month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    async def get_user_quota(self, user_id: int) -> dict[str, Any]:
        """
        استرجاع حالة كوتة السير الذاتية للمستخدم للشهر الحالي.
        يتضمن إعادة الضبط التلقائي للرصيد المجاني شهرياً مع الاحتفاظ بالرصيد الإضافي المشتَرى.
        """
        current_m = self._current_month()
        monthly_limit = settings.cv_monthly_limit

        rs = await self.client.execute(
            "SELECT monthly_used, extra_balance, reset_month FROM user_quotas WHERE user_id = ?",
            [user_id],
        )

        if not rs.rows:
            await self.ensure_user_exists(user_id)
            try:
                await self.client.execute(
                    """
                    INSERT INTO user_quotas (user_id, monthly_used, extra_balance, reset_month, updated_at)
                    VALUES (?, 0, 0, ?, ?)
                    ON CONFLICT(user_id) DO NOTHING
                    """,
                    [user_id, current_m, _now()],
                )
            except Exception as e:
                logger.warning("تعذر إدراج سجل كوتة جديد للمستخدم %s: %s", user_id, e)
            monthly_used = 0
            extra_balance = 0
            reset_month = current_m
        else:
            monthly_used = int(rs.rows[0][0] or 0)
            extra_balance = int(rs.rows[0][1] or 0)
            reset_month = str(rs.rows[0][2] or "")

            # حلول شهر جديد: تصفير الاستهلاك الشهري والاحتفاظ بالرصيد الإضافي المشتَرى
            if reset_month != current_m:
                monthly_used = 0
                reset_month = current_m
                try:
                    await self.client.execute(
                        """
                        UPDATE user_quotas
                        SET monthly_used = 0, reset_month = ?, updated_at = ?
                        WHERE user_id = ?
                        """,
                        [current_m, _now(), user_id],
                    )
                except Exception as e:
                    logger.warning("فشل تحديث تصفير الشهر للكوتة للمستخدم %s: %s", user_id, e)

        remaining_monthly = max(0, monthly_limit - monthly_used)
        total_available = remaining_monthly + extra_balance

        return {
            "user_id": user_id,
            "monthly_used": monthly_used,
            "monthly_limit": monthly_limit,
            "remaining_monthly": remaining_monthly,
            "extra_balance": extra_balance,
            "total_available": total_available,
            "reset_month": reset_month,
        }

    async def consume_user_quota(self, user_id: int) -> bool:
        """
        خصم سيرة ذاتية واحدة من رصيد المستخدم (الشهري أولاً ثم الإضافي).
        تُرجع True عند نجاح الخصم، أو False إذا نفد الرصيد تماماً.
        """
        quota = await self.get_user_quota(user_id)
        if quota["total_available"] <= 0:
            return False

        monthly_used = quota["monthly_used"]
        monthly_limit = quota["monthly_limit"]
        extra_balance = quota["extra_balance"]

        if monthly_used < monthly_limit:
            new_monthly_used = monthly_used + 1
            new_extra = extra_balance
        else:
            new_monthly_used = monthly_used
            new_extra = max(0, extra_balance - 1)

        await self.client.execute(
            """
            UPDATE user_quotas
            SET monthly_used = ?, extra_balance = ?, updated_at = ?
            WHERE user_id = ?
            """,
            [new_monthly_used, new_extra, _now(), user_id],
        )
        return True

    async def add_extra_quota(self, user_id: int, count: int = 10) -> None:
        """إضافة رصيد سير ذاتية إضافية (باقة مدفوعة) لحساب المستخدم."""
        await self.get_user_quota(user_id)  # ضمان وجود السجل وتحديث الشهر
        await self.client.execute(
            """
            UPDATE user_quotas
            SET extra_balance = extra_balance + ?, updated_at = ?
            WHERE user_id = ?
            """,
            [count, _now(), user_id],
        )

    # ------------------------------------------------------------------
    # إعدادات النظام الديناميكية والتحكم بالأسعار (لوحة الأدمن)
    # ------------------------------------------------------------------
    async def get_setting(self, key: str, default: str = "") -> str:
        """استرجاع إعداد ديناميكي من جدول app_settings مع قيمة افتراضية."""
        try:
            rs = await self.client.execute("SELECT value FROM app_settings WHERE key = ?", [key])
            if rs.rows:
                return str(rs.rows[0][0])
        except Exception as e:
            logger.warning("تعذر استرجاع الإعداد %s من app_settings: %s", key, e)
        return default

    async def set_setting(self, key: str, value: str) -> None:
        """حفظ أو تعديل إعداد ديناميكي."""
        await self.client.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            [key, str(value), _now()],
        )

    async def get_docx_prices(self) -> tuple[str, str]:
        """إرجاع أسعار نسخة Word الحالية (USD, SYP)."""
        usd = await self.get_setting("docx_price_usd", settings.docx_price_usd)
        syp = await self.get_setting("docx_price_syp", settings.docx_price_syp)
        return usd, syp

    async def get_cv_quota_price_syp(self) -> str:
        """إرجاع سعر باقة الـ 10 سير ذاتية الإضافية (SYP)."""
        return await self.get_setting("cv_quota_price_syp", settings.cv_quota_price_syp)


db = Database()
