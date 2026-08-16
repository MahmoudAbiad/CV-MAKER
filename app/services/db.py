"""
طبقة الوصول لقاعدة بيانات Turso (libSQL) بشكل غير متزامن بالكامل
لا يتم استخدام أي ORM ثقيل - استعلامات SQL مباشرة عبر libsql-client
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import libsql_client

from app.config import settings


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """غلاف بسيط حول عميل libsql-client غير المتزامن (Client واحد يُعاد استخدامه)."""

    def __init__(self) -> None:
        self._client: Optional[libsql_client.Client] = None

    async def connect(self) -> None:
        if self._client is None:
            self._client = libsql_client.create_client(
                url=settings.turso_db_url,
                auth_token=settings.turso_auth_token,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def client(self) -> libsql_client.Client:
        if self._client is None:
            raise RuntimeError("قاعدة البيانات غير متصلة بعد. استدعِ connect() أولاً.")
        return self._client

    # ------------------------------------------------------------------
    # المستخدمون
    # ------------------------------------------------------------------
    async def upsert_user(self, user_id: int, username: str | None, full_name: str | None) -> None:
        await self.client.execute(
            """
            INSERT INTO users (user_id, username, full_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
            """,
            [user_id, username, full_name, _now()],
        )

    # ------------------------------------------------------------------
    # السير الذاتية
    # ------------------------------------------------------------------
    async def insert_cv_record(self, user_id: int, parsed_json: dict[str, Any], fmt: str) -> int:
        rs = await self.client.execute(
            """
            INSERT INTO cv_records (user_id, parsed_json, format, created_at)
            VALUES (?, ?, ?, ?)
            """,
            [user_id, json.dumps(parsed_json, ensure_ascii=False), fmt, _now()],
        )
        return int(rs.last_insert_rowid)

    async def get_cv_record(self, cv_id: int) -> Optional[dict[str, Any]]:
        rs = await self.client.execute(
            "SELECT id, user_id, parsed_json, format, created_at FROM cv_records WHERE id = ?",
            [cv_id],
        )
        if not rs.rows:
            return None
        row = rs.rows[0]
        return {
            "id": row[0],
            "user_id": row[1],
            "parsed_json": json.loads(row[2]),
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
        cv_record_id: int | None,
    ) -> int:
        now = _now()
        rs = await self.client.execute(
            """
            INSERT INTO payments
                (user_id, amount, currency, receipt_file_id, status, created_at, updated_at, cv_record_id)
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            [user_id, amount, currency, receipt_file_id, now, now, cv_record_id],
        )
        return int(rs.last_insert_rowid)

    async def get_payment(self, payment_id: int) -> Optional[dict[str, Any]]:
        rs = await self.client.execute(
            """
            SELECT id, user_id, amount, currency, receipt_file_id, status,
                   admin_note, created_at, updated_at, cv_record_id
            FROM payments WHERE id = ?
            """,
            [payment_id],
        )
        if not rs.rows:
            return None
        r = rs.rows[0]
        return {
            "id": r[0], "user_id": r[1], "amount": r[2], "currency": r[3],
            "receipt_file_id": r[4], "status": r[5], "admin_note": r[6],
            "created_at": r[7], "updated_at": r[8], "cv_record_id": r[9],
        }

    async def update_payment_status(
        self, payment_id: int, status: str, admin_note: str | None = None
    ) -> None:
        await self.client.execute(
            """
            UPDATE payments
            SET status = ?, admin_note = ?, updated_at = ?
            WHERE id = ?
            """,
            [status, admin_note, _now(), payment_id],
        )

    # ------------------------------------------------------------------
    # إحصائيات لوحة تحكم الأدمن
    # ------------------------------------------------------------------
    async def get_stats(self) -> dict[str, Any]:
        total_users = await self.client.execute("SELECT COUNT(*) FROM users")
        pdf_count = await self.client.execute("SELECT COUNT(*) FROM cv_records WHERE format = 'pdf'")
        docx_count = await self.client.execute("SELECT COUNT(*) FROM cv_records WHERE format = 'docx'")
        pending = await self.client.execute("SELECT COUNT(*) FROM payments WHERE status = 'pending'")
        approved = await self.client.execute("SELECT COUNT(*) FROM payments WHERE status = 'approved'")
        rejected = await self.client.execute("SELECT COUNT(*) FROM payments WHERE status = 'rejected'")
        return {
            "total_users": total_users.rows[0][0],
            "pdf_generated": pdf_count.rows[0][0],
            "docx_generated": docx_count.rows[0][0],
            "payments_pending": pending.rows[0][0],
            "payments_approved": approved.rows[0][0],
            "payments_rejected": rejected.rows[0][0],
        }

    async def get_all_user_ids(self) -> list[int]:
        rs = await self.client.execute("SELECT user_id FROM users")
        return [row[0] for row in rs.rows]


db = Database()
