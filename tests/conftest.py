"""
Fixtures وإعدادات اختبارات pytest لمشروع CV-MAKER
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest

# إضافة مسار المشروع إلى sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ضبط متغيرات بيئة تجريبية آمنة
os.environ.setdefault("BOT_TOKEN", "123456789:ABCDEF_dummy_test_token_for_pytest")
os.environ.setdefault("WEBHOOK_BASE_URL", "https://example.com")
os.environ.setdefault("WEBHOOK_SECRET", "test_webhook_secret_key_123")
os.environ.setdefault("ADMIN_GROUP_ID", "-1001234567890")
os.environ.setdefault("ADMIN_USER_IDS", "999001,999002")
os.environ.setdefault("TURSO_DATABASE_URL", "file:test_pytest.db")
os.environ.setdefault("TURSO_AUTH_TOKEN", "dummy_turso_token")
os.environ.setdefault("UPSTASH_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("GEMINI_API_KEY", "dummy_gemini_api_key")
os.environ.setdefault("SHAM_CASH_CODE", "SC-987654321")


@pytest.fixture
def sample_arabic_cv_data() -> dict[str, Any]:
    return {
        "full_name": "أحمد محمود العلي",
        "title": "مهندس برمجيات أول ومطور سحابي",
        "summary": "مهندس برمجيات ذو خبرة تفوق 5 سنوات في بناء المنظومات الموزعة السحابية وتطوير الحلول البرمجية عالية الأداء باستخدام بايثون وجو، قاد فرقاً تقنية لتحقيق استقرار المنظومة بنسبة 99.9%.",
        "contact": {
            "phone": "+963-991-234567",
            "email": "ahmad.ali@example.com",
            "location": "دمشق، سوريا",
        },
        "experience": [
            {
                "company": "شركة الحلول التقنية المتقدمة",
                "role": "كبير مطوري بايثون",
                "period": "2021 - حتى الآن",
                "bullets": [
                    "تصميم وتطوير بنيات الخدمات المصغرة المعقدة التي تخدم أكثر من 100 ألف مستخدم نشط يومياً.",
                    "تحسين أداء استعلامات قواعد البيانات وتقليل زمن الاستجابة بنسبة 40%.",
                ],
            },
            {
                "company": "مؤسسة النظم الرقمية",
                "role": "مطور ويب متكامل",
                "period": "2019 - 2021",
                "bullets": [
                    "بناء لوحات تحكم متقدمة وواجهات برمجية RESTful عالية الأمان.",
                ],
            },
        ],
        "education": [
            {
                "institution": "جامعة دمشق - كلية هندسة الحاسوب",
                "degree": "إجازة في الهندسة المعلوماتية",
                "period": "2014 - 2019",
            }
        ],
        "skills": [
            "Python",
            "FastAPI",
            "Docker",
            "PostgreSQL",
            "Redis",
            "Git",
            "CI/CD",
        ],
        "languages": [
            "العربية (اللغة الأم)",
            "الإنجليزية (متقدم C1)",
        ],
        "_language": "ar",
        "_style": "enhanced",
    }


@pytest.fixture
def sample_english_cv_data() -> dict[str, Any]:
    return {
        "full_name": "Sarah Jenkins",
        "title": "Senior Product Manager",
        "summary": "Strategic Product Manager with 6+ years driving B2B SaaS lifecycle from discovery to scale. Spearheaded AI-driven customer retention tools growing annual recurring revenue by 35%.",
        "contact": {
            "phone": "+1 (555) 234-5678",
            "email": "sarah.jenkins@example.com",
            "location": "Austin, TX, USA",
        },
        "experience": [
            {
                "company": "CloudScale Innovations Inc.",
                "role": "Lead Product Manager",
                "period": "2022 - Present",
                "bullets": [
                    "Orchestrated cross-functional teams of 18 engineers & designers to deliver enterprise analytics suite.",
                    "Improved net promoter score (NPS) by 24 points within first 6 months post-launch.",
                ],
            }
        ],
        "education": [
            {
                "institution": "University of Texas at Austin",
                "degree": "B.S. in Computer Science & Business Management",
                "period": "2014 - 2018",
            }
        ],
        "skills": [
            "Product Roadmap Strategy",
            "Agile/Scrum",
            "Data Analytics (SQL, Tableau)",
            "User Research & A/B Testing",
        ],
        "languages": [
            "English (Native)",
            "French (Professional Working)",
        ],
        "_language": "en",
        "_style": "enhanced",
    }


@pytest.fixture
def edge_case_cv_data() -> dict[str, Any]:
    """بيانات تحتوي على حالات حدية مثل رموز XML، نصوص خاصة، وحقول فارغة أو جزئية"""
    return {
        "full_name": "R&D Lead <Ahmed & Omar> \"Specialist\"",
        "title": "Full-Stack & Cloud Architect <C++ & Python>",
        "summary": "Experienced with AT&T & Johnson & Johnson. Managed <production> pipelines with 100% SLA & zero downtime.",
        "contact": {
            "phone": "+963-933-112233",
            "email": "dev.test+sub@domain.co.uk",
            "location": "Aleppo & Damascus <Syria>",
        },
        "experience": [
            {
                "company": "A & B Tech Corp <Innovations>",
                "role": "R&D Specialist & Lead Developer",
                "period": "2020 & Beyond",
                "bullets": [
                    "Engineered <microservices> with 99.9% uptime & 50% lower memory usage.",
                    "Handled XML/HTML data <tags> and &amp; characters cleanly.",
                ],
            }
        ],
        "education": [
            {
                "institution": "Faculty of Sci & Tech <University>",
                "degree": "B.Sc. in CS & IT",
                "period": "2015-2019",
            }
        ],
        "skills": [
            "C/C++",
            "HTML & CSS",
            "Python & Django",
            "CI/CD & DevOps",
        ],
        "languages": [
            "Arabic & English",
        ],
        "_language": "ar",
        "_style": "raw",
    }
