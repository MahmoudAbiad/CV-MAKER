"""
خدمة التكامل مع Google Gemini API
- استخراج وهيكلة بيانات السيرة الذاتية من نصوص حرة (Structured JSON Output)
- التحقق الذكي من إيصالات الدفع المالي (Sham Cash receipts) عبر Gemini Multimodal Vision
- إدارة متينة للشبكة والتراجع الأسي (Exponential Backoff with Jitter) عند 429 وأخطاء الخادم 5xx
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

DEFAULT_TEXT_MODEL = "gemini-2.5-flash"
DEFAULT_VISION_MODEL = "gemini-2.5-flash"
FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-3.6-flash", "gemini-2.5-pro", "gemini-3-flash-preview"]

_CV_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["complete", "needs_more_info"],
            "description": "complete إذا كانت المعلومات كافية لبناء سيرة ذاتية مقبولة، وإلا needs_more_info",
        },
        "question": {
            "type": "object",
            "description": "السؤال التكميلي عند وجود نقص. يترك كائناً فارغاً {} عند اكتمال المعلومات.",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "سؤال مباشر وموجز باللهجة السورية المهذبة (أقل من 20 كلمة) دون مقدمات.",
                },
                "question_type": {
                    "type": "string",
                    "enum": ["choice", "open"],
                    "description": "choice للأسئلة المحصورة ذات الخيارات، و open للبيانات المفتوحة.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "خيارات سريعة بين 2 إلى 4 فقط عندما question_type=choice.",
                },
            },
        },
        "follow_up_message": {
            "type": "string",
            "description": "نص مطابق لسؤال question.text كحقل احتياطي، أو نص فارغ عند الاكتمال.",
        },
        "full_name": {"type": "string", "description": "الاسم الكامل لصاحب السيرة الذاتية"},
        "title": {
            "type": "string",
            "description": "المسمى الوظيفي المستهدف أو الاختصاص، عبارة دقيقة وموجزة (أقل من 8 كلمات).",
        },
        "summary": {
            "type": "string",
            "description": "نبذة مهنية تنفيذية مكثفة من 2-3 جمل (35-55 كلمة) بأسلوب الغائب المهني.",
        },
        "contact": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "email": {"type": "string"},
                "location": {"type": "string"},
            },
        },
        "experience": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "period": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "institution": {"type": "string"},
                    "degree": {"type": "string"},
                    "period": {"type": "string"},
                },
            },
        },
        "skills": {"type": "array", "items": {"type": "string"}},
        "languages": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status"],
}

_BASE_PROMPTS = {
    "ar": (
        "أنت خبير محترف ومستشار معتمد في كتابة السير الذاتية بالمعايير العالمية (ATS-compliant). "
        "ستتلقى من المستخدم مدخلات نصية (قد تكون بالعامية أو الفصحى أو الإنجليزية) تصف خبراته ومؤهلاته. "
        "اجمع كل الرسائل كمصدر متكامل. بصرف النظر عن لغة الإدخال، يجب أن تكون كافة مخرجات السيرة "
        "الذاتية النهائية باللغة العربية الفصحى الاحترافية والواضحة."
    ),
    "en": (
        "You are an expert executive resume consultant specializing in crafting professional ATS-compliant CVs. "
        "You will receive input describing experiences, education, and skills. "
        "Synthesize all user inputs into one source. Regardless of input language, every field in the final CV "
        "must be rendered in polished, fluent, and professional English."
    ),
}

_STYLE_INSTRUCTIONS = {
    "ar": {
        "raw": (
            "أسلوب الكتابة: حافظ على صياغة المستخدم وحقائقه الأساسية بأمانة، وصحح فقط الأخطاء النحوية "
            "والإملائية ورتّب البيانات دون مبالغة تسويقية أو اختراع ما لم يذكره."
        ),
        "enhanced": (
            "أسلوب الكتابة: أعد صياغة الخبرات والمحتوى بأسلوب احترافي رفيع يبرز القوة والتميز باستخدام "
            "أفعال إنجاز قوية (قاد، طوّر، أدار، نفّذ، حسّن) مع الالتزام التام بالحقائق دون أي اختلاق."
        ),
    },
    "en": {
        "raw": (
            "Writing style: Preserve the user's authentic facts and wording closely, correcting only "
            "grammar and structural flow without inventing credentials or embellishing."
        ),
        "enhanced": (
            "Writing style: Polish all entries into high-impact, professional resume phrasing using "
            "dynamic action verbs (spearheaded, designed, engineered, implemented, accelerated) while strictly "
            "maintaining factual accuracy."
        ),
    },
}

_COMPLETENESS_INSTRUCTIONS = {
    "ar": (
        "قواعد فحص اكتمال المعلومات:\n"
        "الحد الأدنى لإنشاء سيرة ذاتية قابلة للتصدير:\n"
        "1) الاسم الكامل.\n"
        "2) وسيلة تواصل أساسية واحدة على الأقل (هاتف أو بريد إلكتروني).\n"
        "3) جهة خبرة عملية أو مؤهل تعليمي (شركة أو جامعة/معهد مع إطار زمني تقريبي).\n"
        "4) ثلاث مهارات تقنية أو عملية محددة.\n\n"
        "عند وجود نقص جوهري، اجعل status = 'needs_more_info' واتبع شروط السؤال التكميلي الصارمة:\n"
        "- اطرح سؤالاً واحداً فقط وبشكل مباشر حول العنصر الأكثر أهمية.\n"
        "- ممنوع تلخيص كلام المستخدم أو كتابة مقدمات مثل 'بناءً على ما ذكرت...'.\n"
        "- اللهجة: عامية سورية مهذبة، ودودة وقصيرة جداً (أقل من 20 كلمة).\n"
        "- حدد question_type = 'choice' فقط للأسئلة المحصورة (مثال: نعم/لا) مع خيارين أو ثلاثة، وإلا فاجعله 'open'.\n"
        "- انسخ نفس نص السؤال تماماً إلى follow_up_message.\n\n"
        "إذا كانت البيانات كافية، اجعل status = 'complete' واجعل question = {} و follow_up_message = '' "
        "واستخرج كامل حقول السيرة الذاتية."
    ),
    "en": (
        "Rules for completeness verification:\n"
        "Minimum requirements for a complete CV:\n"
        "1) Full name.\n"
        "2) At least one contact method (phone or email).\n"
        "3) An education or work experience entry with timeframe.\n"
        "4) At least 3 relevant skills.\n\n"
        "If critical data is missing, set status = 'needs_more_info':\n"
        "- Ask exactly ONE direct, polite question in Syrian Arabic (under 20 words) for Telegram chat flow.\n"
        "- Never summarize user input.\n"
        "- Set question_type to 'choice' only for restricted choices, otherwise 'open'.\n"
        "- Mirror the question in follow_up_message.\n\n"
        "If requirements are met, set status = 'complete', question = {}, follow_up_message = '', and extract all fields."
    ),
}

_SUMMARY_INSTRUCTIONS = {
    "ar": (
        "ضوابط صياغة حقل summary (النبذة التنفيذية):\n"
        "1) الهيكل: 2 إلى 3 جمل متماسكة ومترابطة (35-55 كلمة):\n"
        "   - الجملة الأولى: المسمى التخصصي ومجال التركيز المحوري.\n"
        "   - الجملة الثانية: المهارات والأدوات التقنية المطبقة والخبرات المشهودة.\n"
        "   - الجملة الثالثة: القيمة المضافة أو التوجه المهني المنشود.\n"
        "2) الأسلوب: صيغة الغائب التنفيذي، دون ضمائر متكلم ('أنا')، وتجنب تام للكلمات المستهلكة (شغوف، طموح، يسعى للتميز)."
    ),
    "en": (
        "Strict rules for the summary field:\n"
        "1) Structure: 2-3 concise, impactful sentences (35-50 words):\n"
        "   - Sentence 1: Professional identity and core specialization.\n"
        "   - Sentence 2: Key technologies, demonstrated proficiencies, and highlights.\n"
        "   - Sentence 3: Concrete value proposition.\n"
        "2) Tone: Third-person executive voice, completely free of fluff and overused buzzwords."
    ),
}

_FORCE_COMPLETE_SUFFIX = {
    "ar": (
        "\n\nتوجيه استثنائي: المستخدم استنفد عدد جولات الأسئلة المتاحة. "
        "اجعل status = 'complete' حتماً الآن مهما كانت المعلومات، واكتفِ بالبيانات المتاحة دون طلب تفاصيل إضافية."
    ),
    "en": (
        "\n\nMandatory override: The user has reached maximum question attempts. "
        "You MUST set status = 'complete' now and proceed with the available data without asking for anything more."
    ),
}

_FINAL_INSTRUCTION = {
    "ar": (
        "أخرج النتيجة بتنسيق JSON الصارم وفق المخطط المطلوب فقط. "
        "تحذير صارم: يجب أن تحتوي القيم النصية على المحتوى النهائي فقط، دون أي تعليقات أو شروحات عن عملية التدقيق."
    ),
    "en": (
        "Output strictly valid JSON complying with the requested schema. "
        "Do not include any commentary, formatting notes, or conversational meta-text inside field values."
    ),
}

_FOLLOW_UP_LANGUAGE_OVERRIDE = (
    "\n\nتذكير: حقلا question.text و follow_up_message هما وسيلة محادثة في بوت تيليجرام عربي، "
    "لذلك يجب أن يكونا دائماً بالعامية السورية المهذبة حتى لو كان محتوى السيرة الذاتية بالإنجليزية."
)


def _build_system_prompt(language: str, style: str, force_complete: bool) -> str:
    parts = [
        _BASE_PROMPTS.get(language, _BASE_PROMPTS["ar"]),
        _STYLE_INSTRUCTIONS.get(language, _STYLE_INSTRUCTIONS["ar"]).get(
            style, _STYLE_INSTRUCTIONS[language]["enhanced"]
        ),
        _COMPLETENESS_INSTRUCTIONS["ar"],
        _SUMMARY_INSTRUCTIONS.get(language, _SUMMARY_INSTRUCTIONS["ar"]),
    ]
    if language != "ar":
        parts.append(_FOLLOW_UP_LANGUAGE_OVERRIDE)
    if force_complete:
        parts.append(_FORCE_COMPLETE_SUFFIX.get(language, _FORCE_COMPLETE_SUFFIX["ar"]))
    parts.append(_FINAL_INSTRUCTION.get(language, _FINAL_INSTRUCTION["ar"]))
    return "\n\n".join(parts)


def _looks_degenerate(text: str) -> bool:
    """
    يكتشف انحلال النموذج في حلقات تكرار لا نهائية
    مع تجنب الإنذارات الكاذبة الناتجة عن الرموز، الفواصل، أو أرقام الهواتف أو المدود العربية.
    """
    if not text:
        return False
    t = text.strip()
    if len(t) < 15:
        return False

    if re.search(r"([^\W\d_\s\u0640\.\-\=\*\#])\1{7,}", t):
        return True

    if re.search(
        r"(\b[\w\u0600-\u06FF]{2,}\b(?:\s+[\w\u0600-\u06FF]+)?)(?:\s+\1){3,}",
        t,
        flags=re.IGNORECASE,
    ):
        return True

    if re.search(
        r"(\b(?:[\w\u0600-\u06FF]+\s+){2,5}[\w\u0600-\u06FF]+\b)(?:\s+\1){2,}",
        t,
        flags=re.IGNORECASE,
    ):
        return True

    return False


def _clean_text_field(value: Any, max_len: int = 100, field_type: str = "general") -> str:
    """ينظف الحقول النصية القصيرة ويمنع تسرب التعليقات البرمجية وحذف الإيميلات"""
    if not isinstance(value, str):
        return "" if value is None else str(value).strip()

    val = value.strip().splitlines()[0].strip() if value.strip() else ""
    if not val:
        return ""

    if _looks_degenerate(val):
        logger.warning("تم تجاهل قيمة نصية متحللة في حقل %s: %r", field_type, val[:80])
        return ""

    if len(val) > max_len:
        val = val[:max_len].rsplit(" ", 1)[0].strip()

    if field_type == "general":
        if val.count(":") >= 2 or val.count(";") >= 2:
            logger.warning("تم تجاهل نص يحتوي تنسيقات مراجعة مشبوهة: %r", val[:80])
            return ""

    return val


def _clean_email_field(value: Any) -> str:
    """تنظيف مخصص للبريد الإلكتروني دون الإضرار بالنقاط المشروعة"""
    if not isinstance(value, str):
        return ""
    val = value.strip()
    match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", val)
    return match.group(0) if match else ""


def _clean_body_field(value: Any, max_len: int = 1000) -> str:
    """تنظيف الحقول النصية الطويلة"""
    if not isinstance(value, str):
        return ""
    val = value.strip()
    if not val or _looks_degenerate(val):
        return ""
    if len(val) > max_len:
        val = val[:max_len].rsplit(" ", 1)[0].strip()
    return val


def _sanitize_cv_result(result: dict[str, Any]) -> dict[str, Any]:
    """تنظيف شامل وتطهير لبيانات السيرة الذاتية قبل التوليد والتصدير"""
    if "title" in result:
        result["title"] = _clean_text_field(result["title"], max_len=80, field_type="title")

    if "full_name" in result:
        name = _clean_text_field(result["full_name"], max_len=80, field_type="name")
        result["full_name"] = name or str(result.get("full_name", "")).strip()[:80]

    if "summary" in result:
        result["summary"] = _clean_body_field(result["summary"], max_len=800)

    contact = result.get("contact")
    if isinstance(contact, dict):
        if "email" in contact:
            contact["email"] = _clean_email_field(contact["email"])
        if "phone" in contact:
            contact["phone"] = _clean_text_field(contact["phone"], max_len=40, field_type="phone")
        if "location" in contact:
            contact["location"] = _clean_text_field(contact["location"], max_len=80, field_type="location")

    experience = result.get("experience")
    if isinstance(experience, list):
        cleaned_exp = []
        for exp in experience:
            if not isinstance(exp, dict):
                continue
            exp["company"] = _clean_text_field(exp.get("company", ""), max_len=100)
            exp["role"] = _clean_text_field(exp.get("role", ""), max_len=100)
            exp["period"] = _clean_text_field(exp.get("period", ""), max_len=60)
            bullets = exp.get("bullets") or []
            exp["bullets"] = [
                b for b in (_clean_body_field(x, max_len=350) for x in bullets if isinstance(x, str)) if b
            ]
            if exp["company"] or exp["role"] or exp["bullets"]:
                cleaned_exp.append(exp)
        result["experience"] = cleaned_exp

    education = result.get("education")
    if isinstance(education, list):
        cleaned_edu = []
        for edu in education:
            if not isinstance(edu, dict):
                continue
            edu["institution"] = _clean_text_field(edu.get("institution", ""), max_len=100)
            edu["degree"] = _clean_text_field(edu.get("degree", ""), max_len=100)
            edu["period"] = _clean_text_field(edu.get("period", ""), max_len=60)
            if edu["institution"] or edu["degree"]:
                cleaned_edu.append(edu)
        result["education"] = cleaned_edu

    for key, max_l in (("skills", 60), ("languages", 40)):
        items = result.get(key)
        if isinstance(items, list):
            result[key] = [
                c for c in (_clean_text_field(x, max_len=max_l) for x in items if isinstance(x, str)) if c
            ]

    return result


def _has_minimum_cv_content(result: dict[str, Any]) -> bool:
    if not (result.get("full_name") or "").strip():
        return False
    return bool(
        (result.get("summary") or "").strip()
        or result.get("experience")
        or result.get("education")
        or result.get("skills")
    )


_GENERIC_FOLLOWUP_FALLBACK = {
    "ar": "ممكن تزودنا بمعلومات إضافية عن دراستك أو خبراتك السابقة ومهاراتك؟",
    "en": "Could you please share more details about your work experience, education, or skills?",
}


def _sanitize_dialogue_result(result: dict[str, Any], language: str) -> dict[str, Any]:
    fallback = _GENERIC_FOLLOWUP_FALLBACK.get(language, _GENERIC_FOLLOWUP_FALLBACK["ar"])
    question = result.get("question")
    if not isinstance(question, dict):
        question = {}

    raw_text = question.get("text") or result.get("follow_up_message") or ""
    clean_text = raw_text.strip().splitlines()[0].strip() if raw_text.strip() else ""

    if not clean_text or _looks_degenerate(clean_text) or len(clean_text) > 220:
        clean_text = fallback

    question["text"] = clean_text
    options = question.get("options")
    if isinstance(options, list):
        question["options"] = [
            o for o in (_clean_text_field(x, max_len=30) for x in options if isinstance(x, str)) if o
        ][:4]
    else:
        question["options"] = []

    result["question"] = question
    result["follow_up_message"] = clean_text
    return result


def _extract_json_from_response(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("استجابة Gemini النصية فارغة")

    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\})", cleaned, flags=re.DOTALL)
        if match:
            return json.loads(match.group(1))
        raise


async def _call_gemini_with_retry(
    url: str, body: dict[str, Any], max_retries: int = 4
) -> dict[str, Any]:
    """
    استدعاء آمن لـ Gemini API مع تراجع أسي وعشوائية (Exponential Backoff with Jitter)
    ومعالجة صحيحة لكل من HTTP 429 وأخطاء الخادم المؤقتة 5xx.
    """
    base_delay = 1.5
    last_error: Exception | None = None

    async with httpx.AsyncClient(timeout=50.0) as client:
        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.post(url, json=body)

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    sleep_time = float(retry_after) if retry_after and retry_after.isdigit() else base_delay
                    sleep_time += random.uniform(0.2, 0.8)
                    logger.warning("Gemini 429 (Rate Limit) - انتظار %.2f ثانية (محاولة %s/%s)", sleep_time, attempt, max_retries)
                    await asyncio.sleep(sleep_time)
                    base_delay *= 2
                    continue

                if resp.status_code in (500, 502, 503, 504):
                    sleep_time = base_delay + random.uniform(0.3, 1.0)
                    logger.warning(
                        "خطأ خادم مؤقت من Gemini (%s) - إعادة المحاولة بعد %.2f ثانية (محاولة %s/%s)",
                        resp.status_code, sleep_time, attempt, max_retries,
                    )
                    await asyncio.sleep(sleep_time)
                    base_delay *= 2
                    continue

                if 400 <= resp.status_code < 500:
                    logger.error("خطأ غير قابل للإعادة من Gemini (%s): %s", resp.status_code, resp.text[:300])
                    resp.raise_for_status()

                resp.raise_for_status()
                return resp.json()

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                sleep_time = base_delay + random.uniform(0.2, 0.6)
                logger.warning("عطل شبكة أثناء الاتصال بـ Gemini: %s (محاولة %s/%s)", exc, attempt, max_retries)
                await asyncio.sleep(sleep_time)
                base_delay *= 2

            except httpx.HTTPStatusError as exc:
                last_error = exc
                logger.error("فشل استدعاء Gemini HTTP: %s - الاستجابة: %s", exc, exc.response.text[:200])
                break

    raise RuntimeError(f"فشل الاتصال بـ Gemini بعد {max_retries} محاولات: {last_error}")


async def check_and_extract_cv(
    raw_text: str,
    language: str = "ar",
    style: str = "enhanced",
    force_complete: bool = False,
) -> dict[str, Any]:
    if language not in ("ar", "en"):
        language = "ar"
    if style not in ("raw", "enhanced"):
        style = "enhanced"

    preferred_model = getattr(settings, "gemini_text_model", None) or DEFAULT_TEXT_MODEL
    candidate_models = [preferred_model]
    for m in FALLBACK_MODELS:
        if m not in candidate_models:
            candidate_models.append(m)

    system_prompt = _build_system_prompt(language, style, force_complete)

    token_budgets = [16384, 32768]
    max_content_retries = 2

    for content_attempt in range(1, max_content_retries + 1):
        last_finish_reason: str | None = None
        result: dict[str, Any] | None = None

        for attempt, max_tokens in enumerate(token_budgets, start=1):
            generation_config: dict[str, Any] = {
                "responseMimeType": "application/json",
                "responseSchema": _CV_CHECK_SCHEMA,
                "maxOutputTokens": max_tokens,
                "temperature": 0.2,
            }

            body = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": raw_text}]}],
                "generationConfig": generation_config,
            }

            data = None
            last_err = None
            for m_name in candidate_models:
                url = f"{_GEMINI_BASE}/{m_name}:generateContent?key={settings.gemini_api_key}"
                try:
                    data = await _call_gemini_with_retry(url, body)
                    break
                except Exception as e:
                    last_err = e
                    logger.warning("النموذج %s لم ينجح في تحليل CV (%s)، جاري تجربة النموذج التالي...", m_name, e)
                    continue

            if data is None:
                raise RuntimeError(f"تعذر استدعاء أي من نماذج Gemini المتاحة: {last_err}")

            candidate = data.get("candidates", [{}])[0]
            finish_reason = candidate.get("finishReason")
            last_finish_reason = finish_reason

            if finish_reason and finish_reason not in ("STOP",):
                logger.warning(
                    "توقف غير مكتمل من Gemini (finishReason=%s, محاولة %s/%s)",
                    finish_reason, attempt, len(token_budgets)
                )
                continue

            parts = candidate.get("content", {}).get("parts", [])
            text_part = "".join(p.get("text", "") for p in parts)
            if not text_part.strip():
                continue

            try:
                result = _extract_json_from_response(text_part)
                break
            except Exception as err:
                logger.warning("فشل فك شفرة JSON من استجابة Gemini: %s", err)
                continue

        if result is None:
            if content_attempt < max_content_retries:
                logger.warning("إعادة محاولة استخراج محتوى السيرة الذاتية (المحاولة %s)", content_attempt)
                continue
            raise RuntimeError(f"استجابة Gemini غير مكتملة (finishReason={last_finish_reason})")

        if force_complete:
            result["status"] = "complete"

        if result.get("status") != "complete":
            return _sanitize_dialogue_result(result, language)

        result = _sanitize_cv_result(result)

        if _has_minimum_cv_content(result):
            result["_language"] = language
            result["_style"] = style
            return result

        logger.warning("نتيجة Gemini مكتملة اسمياً لكن تفتقر للحد الأدنى من المحتوى، إعادة محاولة...")

    raise RuntimeError("تعذر استخراج محتوى صالح للسيرة الذاتية بعد عدة محاولات")


_RECEIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_receipt": {
            "type": "boolean",
            "description": "true فقط إذا كانت الصورة إيصال تحويل مالي واضح وحقيقي (مثل شام كاش / سيريتل كاش / بيمو).",
        },
        "confidence": {
            "type": "number",
            "description": "نسبة الثقة في الحكم بين 0.0 و 1.0.",
        },
        "transaction_id": {
            "type": "string",
            "description": "رقم العملية أو الحوالة / المعاملة إن وجد بالإيصال، أو نص فارغ.",
        },
        "amount": {
            "type": "string",
            "description": "قيمة المبلغ المحول المكتوب في الإيصال إن وجد، أو نص فارغ.",
        },
        "recipient": {
            "type": "string",
            "description": "اسم أو رقم حساب المستلم الموضح بالإيصال إن وجد، أو نص فارغ.",
        },
        "date_time": {
            "type": "string",
            "description": "تاريخ وتوقيت العملية المكتوب في الإيصال إن وجد، أو نص فارغ.",
        },
        "reason": {
            "type": "string",
            "description": "شرح موجز ودقيق بالعربية لسبب الحكم على الإيصال ومطابقته.",
        },
    },
    "required": ["is_receipt", "confidence", "reason"],
}


async def verify_payment_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """فحص صور إيصالات الدفع (Sham Cash) عبر Gemini Multimodal Vision"""
    preferred_model = getattr(settings, "gemini_vision_model", None) or DEFAULT_VISION_MODEL
    candidate_models = [preferred_model]
    for m in FALLBACK_MODELS:
        if m not in candidate_models:
            candidate_models.append(m)

    target_code = getattr(settings, "sham_cash_code", "")
    prompt = (
        "أنت نظام تدقيق مالي متخصص في فحص إيصالات التحويل عبر تطبيقات الدفع الإلكتروني (خاصة Sham Cash / شام كاش، "
        "أو سيريتل كاش، إم تي إن كاش، أو التطبيقات المصرفية السورية المشابهة).\n"
        "المهمة:\n"
        "1. افحص الصورة بدقة وتأكد هل هي إيصال تحويل مالي مكتمل وناجح فعلاً أم صورة غير ذات صلة.\n"
        "2. استخرج بدقة: رقم العملية (transaction_id)، المبلغ (amount)، المستفيد (recipient)، وتاريخ ووقت العملية (date_time).\n"
        f"3. تحقق إن كان المستفيد أو رقم الكود يتطابق أو يقارب كود المستفيد المطلوب: '{target_code}'.\n"
        "4. أرجع النتيجة حصراً بصيغة JSON وفق المخطط المحدد."
    )

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": base64.b64encode(image_bytes).decode("utf-8"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _RECEIPT_SCHEMA,
            "maxOutputTokens": 2048,
            "temperature": 0.1,
        },
    }

    data = None
    last_err = None
    for m_name in candidate_models:
        url = f"{_GEMINI_BASE}/{m_name}:generateContent?key={settings.gemini_api_key}"
        try:
            data = await _call_gemini_with_retry(url, body)
            break
        except Exception as e:
            last_err = e
            logger.warning("نموذج الرؤية %s لم ينجح في فحص الإيصال (%s)، تجربة التالي...", m_name, e)
            continue

    if data is None:
        logger.error("تعذر فحص الإيصال عبر أي نموذج متاح: %s", last_err)
        return {"is_receipt": True, "confidence": 0.0, "reason": "تعذّر الفحص الآلي، إحالة للمراجعة اليدوية"}

    candidates = data.get("candidates", [])
    if not candidates:
        logger.error("لم يرجع Gemini أي مرشحات في فحص الإيصال")
        return {"is_receipt": False, "confidence": 0.0, "reason": "لم تتوفر استجابة من نموذج التحليل"}

    candidate = candidates[0]
    parts = candidate.get("content", {}).get("parts", [])
    if not parts or "text" not in parts[0]:
        finish_reason = candidate.get("finishReason")
        logger.error("استجابة فحص الإيصال فارغة (finishReason=%s)", finish_reason)
        return {"is_receipt": False, "confidence": 0.0, "reason": f"انتهت الاستجابة بسبب {finish_reason}"}

    text_part = parts[0]["text"]
    try:
        return _extract_json_from_response(text_part)
    except Exception as exc:
        logger.error("فشل قراءة JSON من استجابة فحص الإيصال: %s - النص: %r", exc, text_part[:150])
        return {"is_receipt": False, "confidence": 0.0, "reason": "تعذر تحليل الرد كـ JSON صالح"}
