# =========================================================
# Dockerfile متعدد المراحل - مُحسَّن لذاكرة 512MB على Render
# =========================================================

# ---------- المرحلة 1: بناء الاعتماديات ----------
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ---------- المرحلة 2: صورة التشغيل النهائية ----------
FROM python:3.11-slim

WORKDIR /app

# مستخدم غير جذري (non-root) لأسباب أمنية
RUN useradd --create-home --shell /bin/bash appuser

COPY --from=builder /root/.local /home/appuser/.local
COPY app/ ./app/
COPY assets/ ./assets/

ENV PATH="/home/appuser/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 10000

# عامل واحد فقط (uvicorn worker) للحفاظ على استهلاك ذاكرة منخفض جداً
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10000", "--workers", "1"]
