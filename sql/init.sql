-- =============================================================
-- سكريبت تهيئة قاعدة بيانات Turso (libSQL) المحدّث والمحصن
-- آمن بنسبة 100% لإعادة التشغيل المتكرر (Fully Idempotent)
-- =============================================================

-- تفعيل قيود المفاتيح الأجنبية
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------
-- 1. جدول المستخدمين
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,          -- معرّف تيليجرام الفريد للمستخدم
    username    TEXT,                         -- اسم المستخدم في تيليجرام (اختياري)
    full_name   TEXT,                         -- الاسم الكامل الظاهر
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

-- ---------------------------------------------------------------
-- 2. جدول سير المستخدمين الذاتية (السجلات المُولّدة)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cv_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    parsed_json  TEXT NOT NULL,                -- بيانات السيرة الذاتية بصيغة JSON بعد معالجة Gemini
    format       TEXT NOT NULL CHECK (format IN ('pdf', 'docx')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cv_records_user_id ON cv_records(user_id);
CREATE INDEX IF NOT EXISTS idx_cv_records_format ON cv_records(format);
CREATE INDEX IF NOT EXISTS idx_cv_records_created_at ON cv_records(created_at);

-- ---------------------------------------------------------------
-- 3. جدول المدفوعات (Sham Cash / الدفع الإلكتروني)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    cv_record_id      INTEGER REFERENCES cv_records(id) ON DELETE SET NULL,
    amount            REAL NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'SYP',
    payment_type      TEXT NOT NULL DEFAULT 'docx' CHECK (payment_type IN ('docx', 'cv_quota')),
    receipt_file_id   TEXT NOT NULL,            -- file_id الخاص بصورة الإيصال في تيليجرام
    transaction_id    TEXT,                      -- رقم العملية المستخرج من الإيصال لمنع التكرار
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    admin_note        TEXT,                      -- سبب الرفض أو ملاحظة الأدمن
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_cv_record_id ON payments(cv_record_id);
CREATE INDEX IF NOT EXISTS idx_payments_payment_type ON payments(payment_type);
CREATE INDEX IF NOT EXISTS idx_payments_receipt_file_id ON payments(receipt_file_id);
CREATE INDEX IF NOT EXISTS idx_payments_transaction_id ON payments(transaction_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);

-- ---------------------------------------------------------------
-- 4. جدول كوتة السير الذاتية لكل مستخدم (الحد الشهري والباقات الإضافية)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_quotas (
    user_id             INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    monthly_used        INTEGER NOT NULL DEFAULT 0,
    extra_balance       INTEGER NOT NULL DEFAULT 0,
    reset_month         TEXT NOT NULL,          -- بصيغة YYYY-MM لإعادة التعيين الشهري التلقائي
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_user_quotas_reset_month ON user_quotas(reset_month);

-- ---------------------------------------------------------------
-- 5. جدول إعدادات التطبيق الديناميكية (الأسعار والتحكم)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------
-- 6. مشغّل لتحديث حقل updated_at تلقائياً عند تعديل أي دفعة
-- ---------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_payments_updated_at
AFTER UPDATE ON payments
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at OR NEW.updated_at IS NULL
BEGIN
    UPDATE payments SET updated_at = datetime('now') WHERE id = NEW.id;
END;
