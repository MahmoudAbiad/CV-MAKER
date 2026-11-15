"""
إعدادات التطبيق العامة
يتم تحميل جميع القيم الحساسة من متغيرات البيئة (Environment Variables)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

# تحميل ملف .env المحلي تلقائياً إن وجد
load_dotenv()


def _get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int = 0) -> int:
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    res = []
    for x in raw.split(","):
        x = x.strip()
        if x.isdigit() or (x.startswith("-") and x[1:].isdigit()):
            res.append(int(x))
    return res


@dataclass(frozen=True, slots=True)
class Settings:
    # --- Telegram ---
    bot_token: str = field(default_factory=lambda: _get_env("BOT_TOKEN"))
    webhook_base_url: str = field(default_factory=lambda: _get_env("WEBHOOK_BASE_URL"))
    webhook_path: str = field(default_factory=lambda: _get_env("WEBHOOK_PATH", "/webhook"))
    webhook_secret: str = field(default_factory=lambda: _get_env("WEBHOOK_SECRET"))
    admin_group_id: int = field(default_factory=lambda: _get_int("ADMIN_GROUP_ID", 0))
    admin_user_ids: list[int] = field(default_factory=lambda: _get_int_list("ADMIN_USER_IDS"))

    # --- Turso (libSQL) ---
    turso_db_url: str = field(default_factory=lambda: _get_env("TURSO_DATABASE_URL"))
    turso_auth_token: str = field(default_factory=lambda: _get_env("TURSO_AUTH_TOKEN"))

    # --- Upstash Redis ---
    upstash_redis_url: str = field(default_factory=lambda: _get_env("UPSTASH_REDIS_URL"))

    # --- Gemini ---
    gemini_api_key: str = field(default_factory=lambda: _get_env("GEMINI_API_KEY"))
    gemini_text_model: str = field(default_factory=lambda: _get_env("GEMINI_TEXT_MODEL", "gemini-3.5-flash-lite"))
    gemini_vision_model: str = field(default_factory=lambda: _get_env("GEMINI_VISION_MODEL", "gemini-3.5-flash-lite"))

    # --- Payments (Sham Cash) ---
    sham_cash_code: str = field(default_factory=lambda: _get_env("SHAM_CASH_CODE"))
    docx_price_usd: str = field(default_factory=lambda: _get_env("DOCX_PRICE_USD", "0.20"))
    docx_price_syp: str = field(default_factory=lambda: _get_env("DOCX_PRICE_SYP", "25"))
    qr_image_path: str = field(default_factory=lambda: _get_env("QR_IMAGE_PATH", "./assets/sham_cash_qr.jpeg"))

    # --- Redis queue names ---
    queue_cv_generation: str = "queue:cv_generation"
    queue_receipt_screening: str = "queue:receipt_screening"
    queue_docx_generation: str = "queue:docx_generation"

    def validate_required(self) -> None:
        """التحقق من وجود كافة المتغيرات الحرجة عند بدء التشغيل الفعلي للتطبيق"""
        required_map = {
            "BOT_TOKEN": self.bot_token,
            "WEBHOOK_BASE_URL": self.webhook_base_url,
            "WEBHOOK_SECRET": self.webhook_secret,
            "ADMIN_GROUP_ID": self.admin_group_id,
            "TURSO_DATABASE_URL": self.turso_db_url,
            "TURSO_AUTH_TOKEN": self.turso_auth_token,
            "UPSTASH_REDIS_URL": self.upstash_redis_url,
            "GEMINI_API_KEY": self.gemini_api_key,
            "SHAM_CASH_CODE": self.sham_cash_code,
        }
        missing = [key for key, val in required_map.items() if not val]
        if missing:
            raise RuntimeError(
                f"تعذر تشغيل التطبيق بسبب نقص متغيرات البيئة الإلزامية:\n- "
                + "\n- ".join(missing)
            )


settings = Settings()
