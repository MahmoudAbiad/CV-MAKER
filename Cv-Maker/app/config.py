"""
إعدادات التطبيق العامة
يتم تحميل جميع القيم الحساسة من متغيرات البيئة (Environment Variables)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"متغير البيئة المطلوب غير موجود: {name}")
    return value or ""


def _get_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


@dataclass(frozen=True, slots=True)
class Settings:
    # --- Telegram ---
    bot_token: str = field(default_factory=lambda: _get_env("BOT_TOKEN", required=True))
    webhook_base_url: str = field(default_factory=lambda: _get_env("WEBHOOK_BASE_URL", required=True))
    webhook_path: str = field(default_factory=lambda: _get_env("WEBHOOK_PATH", "/webhook"))
    webhook_secret: str = field(default_factory=lambda: _get_env("WEBHOOK_SECRET", required=True))
    admin_group_id: int = field(default_factory=lambda: int(_get_env("ADMIN_GROUP_ID", required=True)))
    admin_user_ids: list[int] = field(default_factory=lambda: _get_int_list("ADMIN_USER_IDS"))

    # --- Turso (libSQL) ---
    turso_db_url: str = field(default_factory=lambda: _get_env("TURSO_DATABASE_URL", required=True))
    turso_auth_token: str = field(default_factory=lambda: _get_env("TURSO_AUTH_TOKEN", required=True))

    # --- Upstash Redis ---
    upstash_redis_url: str = field(default_factory=lambda: _get_env("UPSTASH_REDIS_URL", required=True))

    # --- Gemini ---
    gemini_api_key: str = field(default_factory=lambda: _get_env("GEMINI_API_KEY", required=True))
    gemini_text_model: str = field(default_factory=lambda: _get_env("GEMINI_TEXT_MODEL", "gemini-2.5-flash"))
    gemini_vision_model: str = field(default_factory=lambda: _get_env("GEMINI_VISION_MODEL", "gemini-2.5-flash"))

    # --- Payments (Sham Cash) ---
    sham_cash_code: str = field(default_factory=lambda: _get_env("SHAM_CASH_CODE", required=True))
    docx_price_usd: str = field(default_factory=lambda: _get_env("DOCX_PRICE_USD", "0.20"))
    docx_price_syp: str = field(default_factory=lambda: _get_env("DOCX_PRICE_SYP", "25"))
    qr_image_path: str = field(default_factory=lambda: _get_env("QR_IMAGE_PATH", "./assets/sham_cash_qr.png"))

    # --- Redis queue names ---
    queue_cv_generation: str = "queue:cv_generation"
    queue_receipt_screening: str = "queue:receipt_screening"
    queue_docx_generation: str = "queue:docx_generation"


settings = Settings()
