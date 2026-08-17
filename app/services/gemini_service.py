"""
خدمة التكامل مع Google Gemini API
- استخراج وهيكلة بيانات السيرة الذاتية من نص عشوائي (Structured JSON Output)
- فحص صور إيصالات الدفع عبر Gemini Vision (Multimodal)
تتضمن معالجة أخطاء الشبكة وحالة تجاوز الحد المسموح (HTTP 429) عبر إعادة المحاولة بتأخير تصاعدي
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# مخطط JSON الذي يجب أن يلتزم به Gemini عند فحص اكتمال المعلومات واستخراج بيانات السيرة الذاتية
# ملاحظة: الحقول المتعلقة بالسيرة الذاتية غير إلزامية هنا (قد تصل جزئية أثناء جولات
# استكمال المعلومات)، بينما status و follow_up_message إلزاميان دوماً لأنهما يحكمان
# منطق الحوار مع المستخدم.
_CV_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["complete", "needs_more_info"],
            "description": "complete إذا كانت المعلومات الأساسية كافية لبناء سيرة ذاتية، وإلا needs_more_info",
        },
        "follow_up_message": {
            "type": "string",
            "description": "رسالة ودّية للمستخدم تطلب المعلومات الناقصة تحديداً؛ نص فارغ إن كانت الحالة complete",
        },
        "full_name": {"type": "string"},
        "title": {
            "type": "string",
            "description": (
                "المسمى الوظيفي المقترح - عبارة قصيرة جداً (أقل من 8 كلمات) فقط، بدون أي شرح أو "
                "تعليق. اتركه فارغاً إن لم تكن واثقاً"
            ),
        },
        "summary": {"type": "string", "description": "ملخص احترافي قصير"},
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
    "required": ["status", "follow_up_message"],
}

# الشخصية الأساسية للمساعد + توجيه لغة الحقول النهائية للسيرة الذاتية
_BASE_PROMPTS = {
    "ar": (
        "أنت مساعد خبير في كتابة السير الذاتية الاحترافية. سيصلك من المستخدم نص أو أكثر "
        "(قد يكون بالعامية أو الفصحى أو حتى بالإنجليزية) يصف خبرته ودراسته ومهاراته، وربما "
        "رسائل متابعة لاحقة تكمّل معلومات ناقصة. اعتبر كل الرسائل المُرسلة من المستخدم مجتمعة "
        "كمصدر واحد للمعلومات. بغض النظر عن لغة النص الأصلي، يجب أن تكون كل الحقول النصية في "
        "الناتج النهائي باللغة العربية الفصحى دائماً."
    ),
    "en": (
        "You are an expert assistant specializing in writing professional CVs. You will receive one or "
        "more messages from the user (possibly casual, dialect-heavy, or even written in Arabic) "
        "describing their experience, education, and skills, and possibly later follow-up messages "
        "completing missing information. Treat all the user's messages together as one combined source "
        "of information. Regardless of the original language, every text field in the final output must "
        "always be in fluent, professional English."
    ),
}

# أسلوب إعادة الصياغة: يتحكم به اختيار المستخدم بين النص كما هو أو تحسين الذكاء الاصطناعي له
_STYLE_INSTRUCTIONS = {
    "ar": {
        "raw": (
            "أسلوب الكتابة المطلوب: حافظ على كلام المستخدم وحقائقه كما هي قدر الإمكان. صحّح فقط "
            "الأخطاء الإملائية والنحوية البسيطة، ونظّم المعلومات ضمن الحقول المطلوبة، دون إضافة "
            "أوصاف أو إنجازات أو مهارات لم يذكرها المستخدم صراحة، ودون استخدام عبارات تسويقية "
            "مبالغ فيها لم ترد في كلامه."
        ),
        "enhanced": (
            "أسلوب الكتابة المطلوب: أعد صياغة كل الحقول النصية بأسلوب احترافي جذاب يستخدم أفعال "
            "حركية قوية (مثل: قاد، طوّر، حسّن، نفّذ، صمّم، ابتكر)، مع الحفاظ التام على صحة "
            "المعلومات كما ذكرها المستخدم دون اختراع وقائع أو إنجازات جديدة لم يذكرها."
        ),
    },
    "en": {
        "raw": (
            "Writing style required: preserve the user's own wording and facts as closely as possible. "
            "Only fix minor spelling/grammar mistakes and organize the information into the required "
            "fields, without adding achievements, skills, or descriptions the user did not explicitly "
            "mention, and without exaggerated marketing language they did not use."
        ),
        "enhanced": (
            "Writing style required: rewrite every text field in a polished, professional tone using "
            "strong action verbs (e.g., led, developed, improved, implemented, designed, pioneered), "
            "while strictly preserving the facts exactly as provided by the user without inventing new "
            "achievements or facts."
        ),
    },
}

# معايير اكتمال المعلومات + أسلوب طرح السؤال التكميلي على المستخدم
_COMPLETENESS_INSTRUCTIONS = {
    "ar": (
        "بالإضافة لما سبق، أنت مسؤول عن التحقق من اكتمال المعلومات الأساسية اللازمة لبناء سيرة "
        "ذاتية مفيدة، وهي:\n"
        "1) الاسم الكامل للمستخدم\n"
        "2) عنصر واحد على الأقل بتفاصيل حقيقية من (خبرة عملية) أو (تعليم/دراسة)\n"
        "3) قائمة مهارات لا تقل عن مهارتين اثنتين\n\n"
        "إن كان أي من هذه العناصر ناقصاً أو غامضاً جداً، اجعل status يساوي \"needs_more_info\" "
        "واكتب في follow_up_message رسالة قصيرة ودّية باللهجة العربية العامية السورية "
        "المحترمة (بدون أي ألفاظ غير لائقة، وبأسلوب لطيف كأنك موظف استقبال محترف)، تشكر "
        "المستخدم بإيجاز على ما أرسله حتى الآن، ثم تطلب منه تحديداً وبوضوح العناصر الناقصة فقط "
        "(لا تكرر السؤال عن معلومات أرسلها المستخدم فعلاً). اجعلها رسالة واحدة قصيرة ومباشرة.\n\n"
        "أما إن كانت جميع العناصر الأساسية الثلاثة متوفرة (حتى لو بشكل مختصر)، اجعل status يساوي "
        "\"complete\"، اترك follow_up_message نصاً فارغاً \"\"، واستخرج بقية الحقول "
        "(full_name, title, summary, contact, experience, education, skills, languages) بأفضل "
        "صياغة ممكنة."
    ),
    "en": (
        "In addition, you are responsible for judging whether the essential information needed to "
        "build a useful CV is complete, namely:\n"
        "1) The user's full name\n"
        "2) At least one item with real details from either (work experience) or (education)\n"
        "3) A skills list with at least two skills\n\n"
        "If any of these is missing or too vague, set status to \"needs_more_info\" and write a short, "
        "friendly, respectful message in follow_up_message (polite conversational tone, like a "
        "professional receptionist) that briefly thanks the user for what they already shared, then "
        "clearly asks only for the specific missing pieces (do not re-ask for information already "
        "given). Keep it to one short, direct message.\n\n"
        "If all three essential elements are present (even briefly), set status to \"complete\", leave "
        "follow_up_message as an empty string \"\", and extract the remaining fields (full_name, title, "
        "summary, contact, experience, education, skills, languages) in the best possible form."
    ),
}

_FORCE_COMPLETE_SUFFIX = {
    "ar": (
        "\n\nملاحظة هامة: المستخدم استنفد عدد محاولات إرسال معلومات إضافية. لذلك يجب أن يكون "
        "status = \"complete\" إلزامياً الآن مهما كانت المعلومات المتوفرة، واملأ الحقول الناقصة "
        "بأقل افتراض معقول أو اتركها فارغة، دون طلب أي معلومة إضافية أخرى."
    ),
    "en": (
        "\n\nImportant note: the user has used up their attempts to send more information. You MUST "
        "set status = \"complete\" now regardless of what information is available, filling any gaps "
        "with minimal reasonable defaults or leaving them empty, without asking for anything else."
    ),
}

_FINAL_INSTRUCTION = {
    "ar": (
        "أرجع النتيجة حصراً وفق مخطط JSON المحدد دون أي نص إضافي خارج الحقول. "
        "تحذير مهم جداً: كل حقل نصي (مثل title و full_name وغيرها) يجب أن يحتوي فقط على "
        "المحتوى النهائي النظيف الجاهز للعرض للمستخدم مباشرة — ممنوع منعاً باتاً كتابة أي "
        "شرح، ملاحظة مراجعة، تعليق على مدى التزامك بالتعليمات، أو أي كلام عن العملية نفسها "
        "داخل قيمة أي حقل (مثال على ما هو ممنوع تماماً: \"Format check done\"، \"Title set "
        "to...\"، \"Corrected Value\"، \"Code Revision\"، إلخ). حقل title تحديداً يجب أن يكون "
        "عبارة قصيرة جداً (بضع كلمات فقط، أقل من 8 كلمات) تصف المسمى الوظيفي أو الوضع "
        "الدراسي/المهني، وإن لم تتوفر معلومة كافية لاستنتاجه بثقة اتركه نصاً فارغاً \"\" بدل "
        "كتابة أي شيء آخر."
    ),
    "en": (
        "Return the result strictly following the given JSON schema, with no extra text outside the "
        "fields. Very important warning: every text field (such as title, full_name, etc.) must contain "
        "ONLY the final, clean content ready to be shown directly to the user — it is strictly forbidden "
        "to write any explanation, review note, commentary about your own compliance with instructions, "
        "or any talk about the process itself inside a field's value (examples of what is strictly "
        "forbidden: \"Format check done\", \"Title set to...\", \"Corrected Value\", \"Code Revision\", "
        "etc.). The title field specifically must be a very short phrase (a few words, under 8 words) "
        "describing the job title or academic/professional status, and if there isn't enough information "
        "to confidently infer it, leave it as an empty string \"\" instead of writing anything else."
    ),
}


def _build_system_prompt(language: str, style: str, force_complete: bool) -> str:
    parts = [
        _BASE_PROMPTS.get(language, _BASE_PROMPTS["ar"]),
        _STYLE_INSTRUCTIONS.get(language, _STYLE_INSTRUCTIONS["ar"]).get(
            style, _STYLE_INSTRUCTIONS[language]["enhanced"]
        ),
        _COMPLETENESS_INSTRUCTIONS.get(language, _COMPLETENESS_INSTRUCTIONS["ar"]),
    ]
    if force_complete:
        parts.append(_FORCE_COMPLETE_SUFFIX.get(language, _FORCE_COMPLETE_SUFFIX["ar"]))
    parts.append(_FINAL_INSTRUCTION.get(language, _FINAL_INSTRUCTION["ar"]))
    return "\n\n".join(parts)


def _clean_short_field(value: Any, max_len: int = 80) -> Any:
    """
    طبقة حماية إضافية على مستوى الكود (بغض النظر عن مدى التزام النموذج بالبرومبت):
    تُطبَّق على الحقول القصيرة (سطر واحد متوقع، مثل title وfull_name) لضمان عدم تسرّب
    أي نص تفسيري/تعليقات مراجعة داخلية من النموذج إلى الناتج النهائي الظاهر للمستخدم.
    - تأخذ أول سطر فقط
    - تقصّ الطول عند الحد الأقصى
    - ترفض القيمة كلياً (ترجعها فارغة) إذا بدت أقرب لنص تفسيري (علامات ':' أو '.' متكررة)
    """
    if not isinstance(value, str):
        return value
    value = value.strip().splitlines()[0].strip() if value.strip() else ""
    if value.count(":") >= 2 or value.count(".") >= 2 or len(value) > max_len:
        logger.warning("تم تجاهل حقل قصير مشبوه من مخرجات Gemini: %r", value[:120])
        return ""
    return value


def _sanitize_cv_result(result: dict[str, Any]) -> dict[str, Any]:
    """ينظّف الحقول المتوقع أن تكون قصيرة قبل استخدامها في توليد PDF/DOCX."""
    if "title" in result:
        result["title"] = _clean_short_field(result["title"], max_len=60)
    if "full_name" in result:
        cleaned_name = _clean_short_field(result["full_name"], max_len=80)
        # الاسم الكامل حقل إلزامي لبناء السيرة الذاتية، فلا نفرغه حتى لو بدا مشبوهاً قليلاً؛
        # نكتفي بأخذ أول سطر وقصّ الطول دون رفضه بالكامل كما نفعل مع title
        if not cleaned_name:
            first_line = str(result["full_name"]).strip().splitlines()[0].strip()
            cleaned_name = first_line[:80].rsplit(" ", 1)[0].strip() if len(first_line) > 80 else first_line
        result["full_name"] = cleaned_name
    return result


async def _call_gemini_with_retry(
    url: str, body: dict[str, Any], max_retries: int = 4
) -> dict[str, Any]:
    """يستدعي Gemini API مع إعادة محاولة تصاعدية عند 429 أو أعطال الشبكة المؤقتة."""
    delay = 1.5
    last_error: Exception | None = None

    async with httpx.AsyncClient(timeout=45.0) as client:
        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.post(url, json=body)
                if resp.status_code == 429:
                    logger.warning("Gemini rate limit (429) - المحاولة %s/%s", attempt, max_retries)
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                logger.warning("خطأ شبكة مع Gemini (محاولة %s/%s): %s", attempt, max_retries, exc)
                await asyncio.sleep(delay)
                delay *= 2
            except httpx.HTTPStatusError as exc:
                last_error = exc
                logger.error("خطأ HTTP من Gemini: %s", exc)
                break

    raise RuntimeError(f"فشل الاتصال بـ Gemini بعد {max_retries} محاولات: {last_error}")


async def check_and_extract_cv(
    raw_text: str,
    language: str = "ar",
    style: str = "enhanced",
    force_complete: bool = False,
) -> dict[str, Any]:
    """
    يرسل النص المتراكم من المستخدم (قد يضم عدة رسائل متتالية) إلى Gemini، والذي يقوم بدورين معاً:
    1) الحكم على اكتمال المعلومات الأساسية (status: complete / needs_more_info) وصياغة سؤال
       متابعة ودّي بالعامية عند النقص (follow_up_message).
    2) استخراج بيانات السيرة الذاتية المهيكلة بأفضل شكل ممكن متى ما توفرت المعلومات.

    language: "ar" أو "en" - لغة محتوى السيرة الذاتية الناتجة.
    style: "raw" (الحفاظ على كلام المستخدم كما هو) أو "enhanced" (تحسين الصياغة بلمسة احترافية).
    force_complete: يُستخدم بعد استنفاد عدد محاولات استكمال المعلومات لإجبار Gemini على
        الاكتفاء بالمتوفر بدل الاستمرار بطلب المزيد إلى ما لا نهاية.
    """
    if language not in ("ar", "en"):
        language = "ar"
    if style not in ("raw", "enhanced"):
        style = "enhanced"

    url = f"{_GEMINI_BASE}/{settings.gemini_text_model}:generateContent?key={settings.gemini_api_key}"
    body = {
        "system_instruction": {
            "parts": [{"text": _build_system_prompt(language, style, force_complete)}]
        },
        "contents": [{"role": "user", "parts": [{"text": raw_text}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": _CV_CHECK_SCHEMA,
            # حد أقصى سخي لتوكينات الناتج: نماذج Gemini 3.x (مثل gemini-3.6-flash)
            # تحسب توكينات "التفكير" الداخلي ضمن نفس حد maxOutputTokens الخاص
            # بالناتج، فإن لم نترك هامشاً كافياً يمكن أن يُقتطع الـ JSON فعلياً
            # (تجربتنا أظهرت سيرة ذاتية تحتوي الاسم فقط دون بقية الحقول).
            "maxOutputTokens": 4096,
            # نموذج Gemini 3.x يتجاهل temperature/top_p/top_k تماماً، والمعامل
            # المعتمد الآن للتحكم بعمق "التفكير" هو thinkingLevel. هذه المهمة
            # استخراج/تصنيف مباشر لا تحتاج تفكيراً عميقاً، لذا نستخدم "low" كي
            # لا تستهلك توكينات التفكير حيز الناتج الفعلي بلا داعٍ.
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    data = await _call_gemini_with_retry(url, body)
    candidate = data["candidates"][0]
    finish_reason = candidate.get("finishReason")
    if finish_reason and finish_reason not in ("STOP",):
        # مؤشر مهم: لو توقف Gemini بسبب MAX_TOKENS (غالباً لأن توكينات "التفكير"
        # الداخلي استهلكت معظم الحد المسموح، فلم يتبقَّ ما يكفي لكتابة كامل
        # حقول السيرة الذاتية) فالنتيجة قد تكون JSON صالح شكلياً لكنه غير مكتمل
        # فعلياً (مثلاً: full_name فقط بدون خبرات/تعليم/مهارات رغم أن المستخدم
        # أرسلها). لا نثق بنتيجة كهذه ونطلب من المستخدم إعادة المحاولة بدل
        # تسليم سيرة ذاتية شبه فارغة بصمت.
        logger.error(
            "توقفت استجابة Gemini قبل الاكتمال (finishReason=%s) - على الأغلب "
            "استهلكت توكينات التفكير الداخلي معظم حد maxOutputTokens",
            finish_reason,
        )
        raise RuntimeError(f"استجابة Gemini غير مكتملة (finishReason={finish_reason})")
    text_part = candidate["content"]["parts"][0]["text"]
    result: dict[str, Any] = json.loads(text_part)

    if force_complete:
        result["status"] = "complete"

    if result.get("status") == "complete":
        result = _sanitize_cv_result(result)
        # نضمّن اللغة والأسلوب داخل البيانات نفسها كي يستخدمهما مولّدا PDF وDOCX لاحقاً
        # دون الحاجة لتعديل مخطط قاعدة البيانات
        result["_language"] = language
        result["_style"] = style

    return result


async def verify_payment_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """
    يستخدم Gemini Vision للتحقق مما إذا كانت الصورة إيصال دفع حقيقي (Sham Cash)
    يُعيد: {"is_receipt": bool, "confidence": float, "reason": str}
    """
    url = f"{_GEMINI_BASE}/{settings.gemini_vision_model}:generateContent?key={settings.gemini_api_key}"
    prompt = (
        "افحص هذه الصورة وحدد إن كانت إيصال تحويل مالي حقيقي (مثل إيصال Sham Cash أو تطبيق تحويلات مشابه) "
        "يحتوي على رقم عملية أو مبلغ أو تاريخ، أم أنها صورة غير متعلقة بالدفع (سكرين شوت عشوائي، صورة شخصية، إلخ). "
        "أرجع النتيجة حصراً بصيغة JSON بالمفاتيح التالية: "
        'is_receipt (boolean), confidence (رقم بين 0 و1), reason (سبب قصير بالعربية).'
    )
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}},
                ],
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": {
                "type": "object",
                "properties": {
                    "is_receipt": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["is_receipt", "confidence", "reason"],
            },
            "maxOutputTokens": 1024,
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    data = await _call_gemini_with_retry(url, body)
    candidate = data["candidates"][0]
    finish_reason = candidate.get("finishReason")
    if finish_reason and finish_reason not in ("STOP",):
        logger.error("توقفت استجابة فحص الإيصال عبر Gemini قبل الاكتمال (finishReason=%s)", finish_reason)
        raise RuntimeError(f"استجابة Gemini غير مكتملة (finishReason={finish_reason})")
    text_part = candidate["content"]["parts"][0]["text"]
    return json.loads(text_part)
