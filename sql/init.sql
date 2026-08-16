-- =============================================================
-- سكريبت تهيئة قاعدة بيانات Turso (libSQL)
-- انسخ هذا الملف بالكامل والصقه في Turso Web Console / SQL Shell
-- =============================================================

-- تفعيل قيود المفاتيح الأجنبية
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------
-- جدول المستخدمين
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,          -- معرّف تيليجرام الفريد للمستخدم
    username    TEXT,                         -- اسم المستخدم في تيليجرام (اختياري)
    full_name   TEXT,                         -- الاسم الكامل الظاهر
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

-- ---------------------------------------------------------------
-- جدول سير المستخدمين الذاتية (السجلات المُولّدة)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cv_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    parsed_json  TEXT NOT NULL,                -- بيانات السيرة الذاتية بصيغة JSON بعد معالجة Gemini
    format       TEXT NOT NULL CHECK (format IN ('pdf', 'docx')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cv_records_user_id ON cv_records(user_id);
CREATE INDEX IF NOT EXISTS idx_cv_records_created_at ON cv_records(created_at);

-- ---------------------------------------------------------------
-- جدول المدفوعات (Sham Cash)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    amount            REAL NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'SYP',
    receipt_file_id   TEXT NOT NULL,            -- file_id الخاص بصورة الإيصال في تيليجرام
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    admin_note        TEXT,                      -- سبب الرفض أو ملاحظة الأدمن
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);

-- ---------------------------------------------------------------
-- ربط سجل الدفعة بسيرة ذاتية معيّنة يجب توليدها بصيغة DOCX بعد الموافقة
-- (عمود اختياري يسهّل معرفة أي CV يخص أي دفعة)
-- ---------------------------------------------------------------
ALTER TABLE payments ADD COLUMN cv_record_id INTEGER REFERENCES cv_records(id);
