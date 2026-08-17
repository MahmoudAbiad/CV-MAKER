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
_CV_CHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["complete", "needs_more_info"],
            "description": "complete إذا كانت المعلومات الأساسية كافية لبناء سيرة ذاتية، وإلا needs_more_info",
        },
        "question": {
            "type": "object",
            "description": (
                "السؤال الوحيد الأهم عن المعلومة الناقصة؛ اتركه ككائن فارغ {} إن كانت الحالة complete. "
                "لا تطرح أكثر من سؤال واحد بكل مرة."
            ),
            "properties": {
                "text": {
                    "type": "string",
                    "description": "نص السؤال بلهجة عربية عامية سورية محترمة، قصير ومباشر",
                },
                "question_type": {
                    "type": "string",
                    "enum": ["choice", "open"],
                    "description": (
                        "choice فقط إذا كانت الإجابة تنحصر منطقياً بخيارات محدودة وواضحة (2-4 خيارات) "
                        "مثل نعم/لا أو الاختيار بين فئات معروفة. open لأي سؤال يحتاج إجابة حرة "
                        "(اسم، تفاصيل خبرة، تواريخ، أرقام تواصل...)"
                    ),
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "خيارات قصيرة جداً (كلمة أو كلمتين) تُعرض كأزرار؛ فقط عندما question_type=choice، بين 2 و4 خيارات",
                },
            },
        },
        "follow_up_message": {
            "type": "string",
            "description": (
                "احتياطي فقط: نفس نص question.text تقريباً، يُستخدم إذا تعذّر عرض الأزرار. "
                "اتركه فارغاً إن كانت الحالة complete."
            ),
        },
        "full_name": {"type": "string"},
        "title": {
            "type": "string",
            "description": (
                "المسمى الوظيفي المقترح - عبارة قصيرة جداً (أقل من 8 كلمات) فقط، بدون أي شرح أو "
                "تعليق. اتركه فارغاً إن لم تكن واثقاً"
            ),
        },
        "summary": {
            "type": "string",
            "description": (
                "نبذة مهنية احترافية (Professional Summary) صيغت بأسلوب قوي ومباشر من 2 إلى 3 جمل. "
                "تتبع هيكلية صريحة: [الهوية/الصفة المهنية المباشرة] + [أبرز الخبرات والمهارات التقنية] + [القيمة المضافة أو الهدف المهني]. "
                "ممنوع استخدام عبارات الحشو الفارغة (مثل 'طموح'، 'يسعى للتميز') أو الجمل المفككة."
            ),
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
    "required": ["status", "question", "follow_up_message"],
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
        "ذاتية مفيدة ومقنعة فعلاً (وليست شبه فارغة)، وهي:\n"
        "1) الاسم الكامل للمستخدم\n"
        "2) وسيلة تواصل واحدة على الأقل (رقم هاتف أو إيميل)\n"
        "3) عنصر واحد على الأقل بتفاصيل حقيقية من (خبرة عملية) أو (تعليم/دراسة)، ويجب أن يتضمن "
        "هذا العنصر اسم الجهة (شركة/جامعة) والفترة الزمنية التقريبية (سنة أو مدة)، وليس مجرد "
        "مسمى عام بلا سياق\n"
        "4) قائمة مهارات لا تقل عن ثلاث مهارات محددة (وليست عامة جداً مثل \"مهارات جيدة\")\n\n"
        "إن كان أي من هذه العناصر ناقصاً أو غامضاً جداً، اجعل status يساوي \"needs_more_info\". "
        "اختر العنصر الناقص الأهم فقط (لا تسأل عن أكثر من شيء واحد بكل مرة) واملأ حقل question:\n"
        "- text: سؤال واحد قصير ومباشر بلهجة عربية عامية سورية محترمة (بدون أي ألفاظ غير لائقة، "
        "وبأسلوب لطيف كأنك موظف استقبال محترف)، لا تكرر السؤال عن معلومة أرسلها المستخدم فعلاً\n"
        "- question_type: اجعلها \"choice\" فقط إذا كانت الإجابة المنطقية تنحصر بخيارات واضحة "
        "ومحدودة (مثال: \"هل عندك خبرة عمل سابقة؟\" -> خيارات [\"عندي خبرة\", \"ما عندي خبرة بعد\"])، "
        "وإلا اجعلها \"open\" (مثال: طلب الاسم، أو تفاصيل الخبرة، أو رقم الهاتف/الإيميل، كلها open)\n"
        "- options: فقط عند choice، من 2 إلى 4 خيارات قصيرة جداً (كلمة أو كلمتين لكل خيار)\n"
        "املأ follow_up_message بنفس نص question.text تقريباً كنسخة احتياطية.\n\n"
        "أما إن كانت جميع العناصر الأربعة الأساسية متوفرة، اجعل status يساوي \"complete\"، اترك "
        "question كائناً فارغاً {} وfollow_up_message نصاً فارغاً \"\"، واستخرج بقية الحقول "
        "(full_name, title, summary, contact, experience, education, skills, languages) بأفضل "
        "صياغة ممكنة."
    ),
    "en": (
        "In addition, you are responsible for judging whether the essential information needed to "
        "build a genuinely useful CV (not a near-empty one) is complete, namely:\n"
        "1) The user's full name\n"
        "2) At least one contact method (phone or email)\n"
        "3) At least one item with real details from either (work experience) or (education), and "
        "that item must include the organization name (company/university) and an approximate time "
        "period (year or duration), not just a bare title with no context\n"
        "4) A skills list with at least three specific skills (not vague ones like \"good skills\")\n\n"
        "If any of these is missing or too vague, set status to \"needs_more_info\". Pick only the "
        "single most important missing item (never ask about more than one thing at a time) and fill "
        "the question field:\n"
        "- text: one short, direct question in respectful conversational Syrian Arabic dialect (never "
        "re-ask for information already given)\n"
        "- question_type: \"choice\" only if the logical answer is naturally limited to clear options "
        "(e.g. \"do you have prior work experience?\" -> options [\"I have experience\", \"Not yet\"]), "
        "otherwise \"open\" (e.g. asking for the name, experience details, phone/email are all open)\n"
        "- options: only when choice, 2 to 4 very short options (a word or two each)\n"
        "Fill follow_up_message with roughly the same text as question.text as a fallback.\n\n"
        "If all four essential elements are present, set status to \"complete\", leave question as an "
        "empty object {} and follow_up_message as an empty string \"\", and extract the remaining "
        "fields (full_name, title, summary, contact, experience, education, skills, languages) in the "
        "best possible form."
    ),
}

# تعليمات مخصصة ودقيقة لحقل summary - تدعم بناء فقرة احترافية متماسكة عبر الأمثلة (Few-Shot Prompting)
_SUMMARY_INSTRUCTIONS = {
    "ar": (
        "تعليمات خاصة بدقة وبلاغة حقل summary (النبذة المهنية):\n"
        "هذا الحقل هو الانطباع الأول في السيرة الذاتية، ويجب أن يُبنى وفق المعادلة التالية:\n"
        "الجملة 1: [الصفة/الهوية المهنية المباشرة] (مثال: 'طالب هندسة بيئية في السنة الأولى بجامعة حلب...')\n"
        "الجملة 2: [أهم المهارات أو مجالات التركيز المحددة التي ذكرها المستخدم]\n"
        "الجملة 3: [القيمة المضافة أو الهدف المهني الحقيقي بأسلوب موثوق]\n\n"
        "قواعد الصياغة الصارمة:\n"
        "- ابدأ بالصفة/المسمى الوظيفي مباشرة بدون مقدمات مثل: 'في جامعة كذا...' أو 'يمتلك فلان...'.\n"
        "- لا تكرر اسم الشركة أو الجامعة بنفس الصياغة الحرفية التي ستظهر في قسم التعليم أو الخبرات.\n"
        "- يمنع استخدام كلمات إنشائية فارغة مثل ('شخصية طموحة'، 'يسعى للنجاح'، 'متميز').\n\n"
        "أمثلة للأسلوب المطلوب للتحويل:\n"
        "• مدخل المستخدم: 'أنا علي بدرس كمبيوتر وعندي خبرة بفلاتر وبدي اشتغل'\n"
        "  الناتج المطلوب: 'مطور تطبيقات هواتف متمرس في بيئة Flutter وSupabase. يمتلك خبرة في بناء واجهات مستخدم سلسة وإدارة قواعد البيانات السحابية، ويسعى لتقديم حلول برمجية عالية الكفاءة.'\n\n"
        "• مدخل المستخدم: 'طالب سنة اولى هندسة بيئة حلب ما عندي خبرة بس بحب التحليل البيئي'\n"
        "  الناتج المطلوب: 'طالب هندسة بيئية في سنته الأولى بجامعة حلب، يركز على مجالات التحليل البيئي واستدامة الموارد. يمتلك شغفاً أكاديمياً بتطبيق الحلول الهندسيّة لحماية البيئة وتطوير المهارات العملية.'"
    ),
    "en": (
        "Special instructions for crafting the 'summary' field:\n"
        "Construct a cohesive 2-3 sentence paragraph following this exact formula:\n"
        "Sentence 1: [Direct Professional/Academic Identity]\n"
        "Sentence 2: [Key Technical Skills or Core Focus Areas mentioned]\n"
        "Sentence 3: [Value Addition or Professional Goal]\n\n"
        "Strict Rules:\n"
        "- Start directly with the professional identity (e.g., 'Software Engineer specializing in...', 'First-year Environmental Engineering student...'). Never start with dangling clauses like 'At Aleppo University...'.\n"
        "- Avoid generic buzzwords ('ambitious', 'hard worker', 'seeking success').\n"
        "- Do not copy-paste the exact same sentence that will appear in education or experience.\n\n"
        "Examples of desired output quality:\n"
        "• Raw input: 'I study computer science and know flutter and supabase looking for job'\n"
        "  Desired output: 'Mobile Application Developer specializing in Flutter and Supabase architectures. Skilled in designing intuitive cross-platform interfaces and managing cloud databases, focused on delivering scalable mobile solutions.'"
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

_FOLLOW_UP_LANGUAGE_OVERRIDE = (
    "\n\nملاحظة إلزامية بخصوص question.text وfollow_up_message وoptions تحديداً: هذه الحقول "
    "هي حوار مباشر مع المستخدم داخل بوت تيليجرام عربي، وليست جزءاً من محتوى السيرة الذاتية "
    "النهائي. لذلك يجب أن تكون دائماً باللهجة العربية العامية السورية المحترمة كما هو موضح "
    "أعلاه، حتى لو كانت لغة حقول السيرة الذاتية الأخرى (مثل summary أو experience) إنجليزية. "
    "لا تكتبها بالإنجليزية أبداً مهما كانت لغة الناتج المطلوبة."
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


def _clean_short_field(value: Any, max_len: int = 80) -> Any:
    """
    طبقة حماية إضافية على مستوى الكود:
    تُطبَّق على الحقول القصيرة لضمان عدم تسرّب أي نص تفسيري من النموذج.
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
    يرسل النص المتراكم من المستخدم إلى Gemini لإجراء التحقق واستخراج بيانات السيرة الذاتية.
    """
    if language not in ("ar", "en"):
        language = "ar"
    if style not in ("raw", "enhanced"):
        style = "enhanced"

    url = f"{_GEMINI_BASE}/{settings.gemini_text_model}:generateContent?key={settings.gemini_api_key}"
    system_prompt = _build_system_prompt(language, style, force_complete)

    token_budgets = [16384, 32768]
    last_finish_reason: str | None = None
    result: dict[str, Any] | None = None

    for attempt, max_tokens in enumerate(token_budgets, start=1):
        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": raw_text}]}],
            "generationConfig": {
                "response_mime_type": "application/json",
                "response_schema": _CV_CHECK_SCHEMA,
                "maxOutputTokens": max_tokens,
            },
        }
        data = await _call_gemini_with_retry(url, body)
        candidate = data["candidates"][0]
        finish_reason = candidate.get("finishReason")
        last_finish_reason = finish_reason

        if finish_reason and finish_reason not in ("STOP",):
            logger.warning(
                "توقفت استجابة Gemini قبل الاكتمال (finishReason=%s, maxOutputTokens=%s, "
                "محاولة %s/%s, language=%s)",
                finish_reason, max_tokens, attempt, len(token_budgets), language,
            )
            continue

        parts = candidate.get("content", {}).get("parts", [])
        text_part = "".join(p.get("text", "") for p in parts)
        if not text_part.strip():
            logger.warning(
                "استجابة Gemini فارغة رغم finishReason=STOP (محاولة %s/%s, language=%s)",
                attempt, len(token_budgets), language,
            )
            continue

        result = json.loads(text_part)
        break

    if result is None:
        raise RuntimeError(f"استجابة Gemini غير مكتملة (finishReason={last_finish_reason})")

    if force_complete:
        result["status"] = "complete"

    if result.get("status") == "complete":
        result = _sanitize_cv_result(result)
        result["_language"] = language
        result["_style"] = style

    return result


async def verify_payment_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """
    يستخدم Gemini Vision للتحقق من إيصالات الدفع والتحويلات المالية.
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