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
import re
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# مخطط JSON الذي يجب أن يلتزم به Gemini عند فحص اكتمال المعلومات واستخراج بيانات السيرة الذاتية[cite: 1]
# ملاحظة: الحقول المتعلقة بالسيرة الذاتية غير إلزامية هنا (قد تصل جزئية أثناء جولات[cite: 1]
# استكمال المعلومات)، بينما status و follow_up_message إلزاميان دوماً لأنهما يحكمان[cite: 1]
# منطق الحوار مع المستخدم.[cite: 1]
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
                "السؤال التكميلي الوحيد عند النقص. اتركه كائناً فارغاً {} تماماً إذا كانت الحالة complete. "
                "لا تطرح أكثر من سؤال واحد بكل مرة."
            ),
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "جملة استفهامية واحدة فقط ومباشرة بدون أي مقدمات أو تلخيص لكلام المستخدم "
                        "(أقل من 20 كلمة، عامية سورية مهذبة ولطيفة)."
                    ),
                },
                "question_type": {
                    "type": "string",
                    "enum": ["choice", "open"],
                    "description": (
                        "choice فقط للأسئلة المحدودة والواضحة (2-3 خيارات مثل: نعم/لا). "
                        "open لأي بيانات تفصيلية (اسم، شركة، جامعة، وسيلة تواصل، مهارات)."
                    ),
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "خيارات موجزة جداً (كلمة أو كلمتين للزر) فقط عندما question_type=choice، بين 2 و4 خيارات",
                },
            },
        },
        "follow_up_message": {
            "type": "string",
            "description": (
                "احتياطي فقط: نسخة مطابقة تماماً لنص question.text. "
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
                "نبذة مهنية تنفيذية مكثفة من 2-3 جمل (35-55 كلمة) تتبع هيكل: "
                "(الهوية المهنية المباشرة + أبرز الأدوات والمهارات المطبقة + القيمة المضافة/الهدف) "
                "بصيغة الغائب التنفيذي ودون أي كلمات حشو إنشائية."
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

# الشخصية الأساسية للمساعد + توجيه لغة الحقول النهائية للسيرة الذاتية[cite: 1]
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

# أسلوب إعادة الصياغة: يتحكم به اختيار المستخدم بين النص كما هو أو تحسين الذكاء الاصطناعي له[cite: 1]
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

# معايير اكتمال المعلومات + أسلوب طرح السؤال التكميلي على المستخدم[cite: 1]
_COMPLETENESS_INSTRUCTIONS = {
    "ar": (
        "قواعد التحقق من اكتمال المعلومات وطرح الأسئلة:\n"
        "العناصر الأساسية المطلوبة لبناء سيرة ذاتية مفيدة:\n"
        "1) الاسم الكامل للمستخدم.\n"
        "2) وسيلة تواصل واحدة على الأقل (رقم هاتف أو إيميل).\n"
        "3) جهة عمل أو دراسة محددة مع فترة زمنية تقريبية (شركة أو جامعة/معهد).\n"
        "4) ثلاث مهارات محددة على الأقل.\n\n"
        "إذا كان هناك أي عنصر ناقص أو غامض، اجعل status = 'needs_more_info' والتزم بالقواعد الصارمة التالية لحقل question:\n"
        "- السؤال المباشر فقط: اكتب جملة استفهامية واحدة فقط تسأل عن العنصر الناقص الأهم دون أي مقدمات أو شروحات.\n"
        "- ممنوع منعاً باتاً تلخيص ما كتبه المستخدم، وممنوع كتابة عبارات مثل 'بناءً على ما ذكرت...' أو 'لقد ذكرت كذا ولكن ينقصنا...'.\n"
        "- اللهجة والطول: عامية سورية مهذبة ولطيفة وقصيرة جداً (أقل من 20 كلمة).\n"
        "- أمثلة على الأسئلة المقبولة:\n"
        "  * 'يا ريت تذكرلنا شو اسم الجامعة أو المعهد وتاريخ التخرج التقريبي؟'\n"
        "  * 'ممكن اسم الشركة يلي اشتغلت فيها ومسماك الوظيفي؟'\n"
        "  * 'يا ريت تزودنا برقم هاتف أو إيميل للتواصل معك.'\n"
        "  * 'شو أبرز المهارات أو البرامج يلي بتتقنها بمجال شغلك؟'\n"
        "- خيارات الأزرار: اجعل question_type = 'choice' فقط للأسئلة ذات الخيارات المحصورة والواضحة (مثال: 'عندك خبرة عمل سابقة؟' -> ['عندي خبرة', 'ما عندي خبرة بعد'])، وباقي الأسئلة دائماً 'open'.\n"
        "اجعل follow_up_message مطابقة تماماً لنص question.text.\n\n"
        "إذا كانت البيانات كاملة، اجعل status = 'complete'، واجعل question = {} و follow_up_message = '' واستخرج بقية الحقول بأفضل صياغة ممكنة."
    ),
    "en": (
        "Rules for completeness check and follow-up generation:\n"
        "Essential elements needed for the CV:\n"
        "1) Full name.\n"
        "2) At least one contact method (phone or email).\n"
        "3) A specific education or experience entity with a timeframe.\n"
        "4) At least three specific skills.\n\n"
        "If anything is missing, set status = 'needs_more_info' with strict question rules:\n"
        "- Single direct question: Write ONLY one short question asking for the single most important missing item.\n"
        "- Strictly NEVER summarize user input or explain what was received.\n"
        "- Tone: Respectful, polite Syrian Arabic dialect, under 20 words.\n"
        "- Set question_type = 'choice' only for binary or simple defined choices, otherwise 'open'.\n"
        "Fill follow_up_message with the exact text of question.text.\n\n"
        "If all four elements are present, set status = 'complete', question = {}, follow_up_message = '', and extract the remaining fields."
    ),
}

# تعليمات مخصصة لحقل summary لضمان نبذة تنفيذية قوية ومترابطة (Executive Professional Summary)[cite: 1]
_SUMMARY_INSTRUCTIONS = {
    "ar": (
        "تعليمات حاسمة لصياغة حقل summary (النبذة المهنية / Professional Summary):\n"
        "يجب أن تُكتب النبذة بأسلوب تنفيذي موجز (Executive Summary Tone) يترك انطباعاً أولياً احترافياً، بالاعتماد على الهيكل التالي:\n"
        "1) الهيكل المطلوب (2 إلى 3 جمل مترابطة، بين 35 إلى 55 كلمة):\n"
        "   - الجملة الأولى (الهوية المهنية): تبدأ مباشرة بالمسمى الوظيفي أو التخصص الأكاديمي مع تحديد مجال التركيز الأساسي (مثال: 'مهندس برمجيات متخصص في تطوير الأنظمة الخلفية وبناء واجهات البرمجة...' أو 'طالب هندسة حواسيب وشبكات يركز على إدارة البنية التحتية...').\n"
        "   - الجملة الثانية (المهارات المحورية والأدوات): تلخيص أبرز القدرات التقنية والأدوات العملية والمشاريع الملموسة التي ذكرها المستخدم فعلياً دون سرد عشوائي.\n"
        "   - الجملة الثالثة (القيمة المضافة أو الهدف المهني): جملة ختامية توضح القيمة التي يقدمها أو التوجه المهني المستقبلي بشكل واقعي وعملي.\n\n"
        "2) ضوابط الصياغة والأسلوب:\n"
        "   - استخدام أسلوب الغائب التنفيذي المباشر؛ ممنوع استخدام ضمائر المتكلم ('أنا'، 'قمت') وممنوع البدايات الركيكة ('هو خريج...', 'في هذا المجال...', 'شاب يسعى...').\n"
        "   - حظر كامل للعبارات الإنشائية المستهلكة (Buzzwords) مثل: (شغوف، طموح، يسعى للتميز، يعشق التحدي، ملتزم بالنجاح، يعمل تحت الضغط).\n"
        "   - التلخيص والربط وليس النسخ: لا تكرر التواريخ وأسماء الشركات حرفياً كما هي في قسمي الخبرة والتعليم، بل ادمج المحتوى في سياق مهني سلس.\n"
        "   - التكيف مع مستوى الخبرة: للطلاب/الخريجين الجدد يتم التركيز على المشاريع والمهارات التقنية والجاهزية لسوق العمل؛ ولذوي الخبرة يتم التركيز على المجالات التخصصية والخبرات العملية.\n"
        "   - في حال كانت البيانات المدخلة شحيحة جداً، اكتب جملتين مركزتين بدقة تامة بدلاً من اختراع تفاصيل أو التوسع بحشو إنشائي."
    ),
    "en": (
        "Strict guidelines for crafting the summary field (Professional Summary):\n"
        "The summary must follow an executive-level standard—concise, impactful, ATS-friendly, and action-oriented:\n"
        "1) Structure (2 to 3 cohesive sentences, ~35-50 words):\n"
        "   - Sentence 1 (Professional Identity): Start directly with the professional title/academic specialization and primary domain focus (e.g., 'Software Engineer specializing in backend architecture and API integration...' or 'Computer & Network Engineering student with a focus on system infrastructure...').\n"
        "   - Sentence 2 (Core Competencies & Tooling): Synthesize standout technical capabilities, key frameworks, and proven project achievements explicitly drawn from user input.\n"
        "   - Sentence 3 (Value Proposition / Objective): A focused closing statement detailing applied value or strategic career direction without generic fluff.\n\n"
        "2) Tone & Style Rules:\n"
        "   - Use implied third-person / executive active voice (no 'I', 'me', 'my', or 'he/she is a...').\n"
        "   - Strictly ban generic buzzwords and clichés (e.g., 'passionate', 'hard-working', 'results-driven', 'motivated individual', 'team player', 'striving for excellence').\n"
        "   - Synthesize rather than duplicate: Do not mechanically repeat raw dates, company names, or degree certificates found in the experience or education sections.\n"
        "   - Tailor by career stage: For students/entry-level, highlight applied coursework, technical projects, and core toolchains; for experienced roles, emphasize scope of work and domain impact.\n"
        "   - If input data is minimal, produce two crisp, accurate sentences rather than padding with ungrounded claims."
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
        # نستخدم دوماً نسخة العربية من تعليمات الاكتمال، لأن follow_up_message حوار مع[cite: 1]
        # المستخدم يجب أن يبقى بالعامية السورية بغض النظر عن لغة السيرة الذاتية المطلوبة[cite: 1]
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
    كاشف عام لانحلال مخرجات النموذج إلى حلقات تكرار (loop degeneration) - سلوك
    ملاحظ فعلياً في اللوغز مع بعض النماذج الخفيفة (flash-lite) عند مواجهة
    برومبت طويل نسبياً: تكرار نفس الكلمة/العبارة عشرات المرات، أو تكرار حرف
    واحد (مثل سلسلة أصفار) بدل توليد محتوى فعلي. لا علاقة له بطول النص العادي
    الطبيعي (فقرة نبذة مهنية طويلة مثلاً)، فقط بأنماط التكرار غير الطبيعية.
    """
    if not text:
        return False
    t = text.strip()
    if len(t) < 12:
        return False
    # نفس الحرف يتكرر أكثر من 6 مرات متتالية (0000000...، !!!!!!!، إلخ)
    if re.search(r"(.)\1{6,}", t):
        return True
    # عبارة قصيرة (كلمة أو كلمتين) تتكرر 3 مرات متتالية على الأقل
    if re.search(r"(\b(?:[\w\u0600-\u06FF]+\s+){0,1}[\w\u0600-\u06FF]+\b)(?:\s+\1){2,}", t, flags=re.IGNORECASE):
        return True
    # عبارة أطول (3-6 كلمات) تتكرر مرتين متتاليتين على الأقل - كافٍ للدلالة على
    # حلقة تكرار حتى لو لم تكتمل ثلاث مرات كاملة قبل قطع الرد (finishReason)
    if re.search(r"(\b(?:[\w\u0600-\u06FF]+\s+){2,5}[\w\u0600-\u06FF]+\b)(?:\s+\1){1,}", t, flags=re.IGNORECASE):
        return True
    return False


def _clean_short_field(value: Any, max_len: int = 80) -> Any:
    """
    طبقة حماية إضافية على مستوى الكود (بغض النظر عن مدى التزام النموذج بالبرومبت):
    تُطبَّق على الحقول القصيرة (سطر واحد متوقع، مثل title وfull_name) لضمان عدم تسرّب
    أي نص تفسيري/تعليقات مراجعة داخلية من النموذج إلى الناتج النهائي الظاهر للمستخدم.
    - تأخذ أول سطر فقط
    - تقصّ الطول عند الحد الأقصى
    - ترفض القيمة كلياً (ترجعها فارغة) إذا بدت أقرب لنص تفسيري (علامات ':' أو '.' متكررة)
      أو إذا كانت متحللة لحلقة تكرار (انظر _looks_degenerate)
    """
    if not isinstance(value, str):
        return value
    value = value.strip().splitlines()[0].strip() if value.strip() else ""
    if value.count(":") >= 2 or value.count(".") >= 2 or len(value) > max_len or _looks_degenerate(value):
        logger.warning("تم تجاهل حقل قصير مشبوه من مخرجات Gemini: %r", value[:120])
        return ""
    return value


def _clean_body_field(value: Any, max_len: int = 900) -> Any:
    """
    مثل _clean_short_field لكن للحقول الطويلة نسبياً (summary، bullets، إلخ):
    لا نرفض النص لمجرد احتوائه عدة نقاط/فواصل (طبيعي بفقرة كاملة)، فقط نرفضه
    إذا تحلّل لحلقة تكرار واضحة أو تجاوز طولاً غير معقول لحقل من المفترض أن
    يكون موجزاً.
    """
    if not isinstance(value, str):
        return value
    value = value.strip()
    if not value:
        return ""
    if _looks_degenerate(value):
        logger.warning("تم تجاهل حقل نصي متحلل (تكرار) من مخرجات Gemini: %r", value[:160])
        return ""
    if len(value) > max_len:
        value = value[:max_len].rsplit(" ", 1)[0].strip()
    return value


def _sanitize_cv_result(result: dict[str, Any]) -> dict[str, Any]:
    """ينظّف كل الحقول النصية (على مستوياتها المتداخلة) قبل استخدامها في توليد PDF/DOCX،
    لمنع تسرّب أي نص تفسيري أو مخرجات متحللة (حلقات تكرار) من Gemini إلى الملف النهائي."""
    if "title" in result:
        result["title"] = _clean_short_field(result["title"], max_len=60)

    if "full_name" in result:
        cleaned_name = _clean_short_field(result["full_name"], max_len=80)
        # الاسم الكامل حقل إلزامي لبناء السيرة الذاتية، فلا نفرغه حتى لو بدا مشبوهاً قليلاً؛
        # نكتفي بأخذ أول سطر وقصّ الطول دون رفضه بالكامل كما نفعل مع title - إلا إذا كان
        # متحللاً فعلياً لحلقة تكرار، حينها نفرغه أيضاً لأنه غير صالح للعرض بأي شكل
        if not cleaned_name and not _looks_degenerate(str(result["full_name"])):
            first_line = str(result["full_name"]).strip().splitlines()[0].strip()
            cleaned_name = first_line[:80].rsplit(" ", 1)[0].strip() if len(first_line) > 80 else first_line
        result["full_name"] = cleaned_name

    if "summary" in result:
        result["summary"] = _clean_body_field(result["summary"], max_len=700)

    contact = result.get("contact")
    if isinstance(contact, dict):
        for key in ("phone", "email", "location"):
            if key in contact:
                contact[key] = _clean_short_field(contact[key], max_len=100)

    experience = result.get("experience")
    if isinstance(experience, list):
        cleaned_experience = []
        for exp in experience:
            if not isinstance(exp, dict):
                continue
            exp["company"] = _clean_short_field(exp.get("company", ""), max_len=100)
            exp["role"] = _clean_short_field(exp.get("role", ""), max_len=100)
            exp["period"] = _clean_short_field(exp.get("period", ""), max_len=60)
            bullets = exp.get("bullets") or []
            exp["bullets"] = [
                b for b in (_clean_body_field(x, max_len=300) for x in bullets if isinstance(x, str)) if b
            ]
            # نتجاهل السطر بالكامل فقط إذا لم يبقَ منه أي شيء مفيد بعد التنظيف
            if exp["company"] or exp["role"] or exp["bullets"]:
                cleaned_experience.append(exp)
        result["experience"] = cleaned_experience

    education = result.get("education")
    if isinstance(education, list):
        cleaned_education = []
        for edu in education:
            if not isinstance(edu, dict):
                continue
            edu["institution"] = _clean_short_field(edu.get("institution", ""), max_len=100)
            edu["degree"] = _clean_short_field(edu.get("degree", ""), max_len=100)
            edu["period"] = _clean_short_field(edu.get("period", ""), max_len=60)
            if edu["institution"] or edu["degree"]:
                cleaned_education.append(edu)
        result["education"] = cleaned_education

    for list_key, max_len in (("skills", 60), ("languages", 40)):
        items = result.get(list_key)
        if isinstance(items, list):
            result[list_key] = [
                c for c in (_clean_short_field(x, max_len=max_len) for x in items if isinstance(x, str)) if c
            ]

    return result


def _has_minimum_cv_content(result: dict[str, Any]) -> bool:
    """
    بوابة حماية أخيرة قبل إرسال أي ملف للمستخدم: حتى لو التزم Gemini بصيغة JSON
    الصحيحة وأعاد status='complete'، هذا لا يضمن أنه فعلاً ملأ محتوى السيرة
    الذاتية (بعض الحقول غير إلزامية بالمخطط لأسباب أخرى، وقد تنحل لحلقة تكرار
    فتُفرَّغ في _sanitize_cv_result). لذلك نتحقق برمجياً أنه تبقّى محتوى فعلي
    قبل المتابعة لتوليد الملف - بدل إرسال سيرة ذاتية شبه فارغة (فيها الاسم فقط
    أو حتى بدونه) للمستخدم.
    """
    if not (result.get("full_name") or "").strip():
        return False
    has_body = bool(
        (result.get("summary") or "").strip()
        or result.get("experience")
        or result.get("education")
        or result.get("skills")
    )
    return has_body


# رسالة احتياطية عامة تُستخدم بدل أي سؤال متابعة يصل متحللاً/طويلاً بشكل غير
# طبيعي (لوحظ فعلياً مع أكثر من نموذج: أحياناً حلقة تكرار كلمات، وأحياناً -كما
# حصل مع gemini-3.6-flash- انحراف الرد لفقرة مجاملات طويلة جداً رغم أن التعليمات
# تطلب صراحة جملة واحدة تحت 20 كلمة). هذا الحقل يظهر مباشرة للمستخدم داخل
# تيليجرام، لذلك يجب ضبطه بصرامة على مستوى الكود بغض النظر عن التزام النموذج.
_GENERIC_FOLLOWUP_FALLBACK = {
    "ar": "ممكن تزودني بمعلومات أكتر عن خبراتك أو دراستك أو مهاراتك؟",
    "en": "Could you share a bit more about your experience, education, or skills?",
}


def _clean_dialogue_text(value: Any, language: str, max_len: int = 220) -> str:
    """ينظّف حقول الحوار المباشر مع المستخدم (question.text وfollow_up_message):
    يأخذ أول سطر فقط، ويستبدل القيمة كلياً برسالة احتياطية عامة إذا انحلّت
    لحلقة تكرار أو تجاوزت طولاً غير معقول لسؤال متابعة واحد مفترض أن يكون
    قصيراً جداً (أقل من 20 كلمة حسب التعليمات)."""
    fallback = _GENERIC_FOLLOWUP_FALLBACK.get(language, _GENERIC_FOLLOWUP_FALLBACK["ar"])
    if not isinstance(value, str) or not value.strip():
        return fallback
    text = value.strip()
    if _looks_degenerate(text) or len(text) > max_len:
        logger.warning(
            "تم استبدال حقل حوار متحلل/طويل جداً من مخرجات Gemini برسالة احتياطية عامة "
            "(الطول الأصلي=%s): %r", len(text), text[:150],
        )
        return fallback
    first_line = text.splitlines()[0].strip()
    return first_line or fallback


def _sanitize_dialogue_result(result: dict[str, Any], language: str) -> dict[str, Any]:
    """يُطبَّق على استجابات status='needs_more_info' حصراً: ينظّف question.text
    وquestion.options وfollow_up_message قبل عرضها مباشرة للمستخدم داخل تيليجرام،
    بشكل مستقل تماماً عن تنظيف حقول محتوى السيرة الذاتية (_sanitize_cv_result)."""
    question = result.get("question") or {}
    if not isinstance(question, dict):
        question = {}

    clean_text = _clean_dialogue_text(question.get("text"), language)
    question["text"] = clean_text

    options = question.get("options")
    if isinstance(options, list):
        question["options"] = [
            c for c in (_clean_short_field(o, max_len=30) for o in options if isinstance(o, str)) if c
        ][:4]
    else:
        question["options"] = []

    result["question"] = question
    result["follow_up_message"] = _clean_dialogue_text(
        result.get("follow_up_message") or clean_text, language
    )
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
    system_prompt = _build_system_prompt(language, style, force_complete)

    # حد توكينات الناتج: بعض نماذج Gemini (السلسلة 3.x خصوصاً) قد تحسب توكينات
    # التفكير الداخلي ضمن maxOutputTokens، ولوحظ فعلياً في اللوغز أن نماذج
    # flash-lite (تفكيرها معطّل افتراضياً) قد "تتحلل" لحلقات تكرار طويلة تستهلك
    # كامل الحد المسموح بدل توليد محتوى فعلي عند مواجهة برومبت طويل ومعقّد. لذلك
    # نفعّل thinkingConfig بمستوى منخفض بدل تركه بلا ضبط: هذا يعطي النموذج "خطوة
    # تفكير" قصيرة قبل الكتابة فيقلل احتمال الانحلال، دون كلفة توكينات كبيرة.
    token_budgets = [16384, 32768]
    max_content_retries = 2  # عدد محاولات كاملة إضافية إذا رجع status=complete لكن بدون محتوى حقيقي

    for content_attempt in range(1, max_content_retries + 1):
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
                    "thinkingConfig": {"thinkingLevel": "low"},
                },
            }
            data = await _call_gemini_with_retry(url, body)
            candidate = data["candidates"][0]
            finish_reason = candidate.get("finishReason")
            last_finish_reason = finish_reason

            if finish_reason and finish_reason not in ("STOP",):
                logger.warning(
                    "توقفت استجابة Gemini قبل الاكتمال (finishReason=%s, maxOutputTokens=%s, "
                    "محاولة %s/%s, language=%s) - غالباً انحلال لحلقة تكرار أو استهلاك توكينات "
                    "تفكير؛ سنعيد المحاولة بحد أعلى إن أمكن",
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
            if content_attempt < max_content_retries:
                logger.warning(
                    "فشلت كل محاولات الحصول على استجابة مكتملة من Gemini (محاولة محتوى %s/%s) "
                    "- سنعيد الكرة من الصفر", content_attempt, max_content_retries,
                )
                continue
            raise RuntimeError(f"استجابة Gemini غير مكتملة (finishReason={last_finish_reason})")

        if force_complete:
            result["status"] = "complete"

        if result.get("status") != "complete":
            # لا حاجة للتحقق من اكتمال المحتوى - ما زلنا بمرحلة الأسئلة التكميلية،
            # لكن يجب تنظيف نص السؤال قبل عرضه مباشرة للمستخدم (انظر التعليق أعلى
            # _sanitize_dialogue_result - هذا الحقل عُرضة لانحراف/تحلل حتى مع
            # نماذج أقوى، وليس فقط مشكلة نماذج flash-lite الخفيفة)
            return _sanitize_dialogue_result(result, language)

        result = _sanitize_cv_result(result)

        if _has_minimum_cv_content(result):
            result["_language"] = language
            result["_style"] = style
            return result

        # وصل status=complete لكن بعد التنظيف لم يتبقَّ محتوى فعلي كافٍ (حلقة تكرار
        # التهمت الحقول المهمة، أو تجاهلها النموذج). لا نرسل هذا للمستخدم أبداً؛
        # نعيد المحاولة كاملة إن أمكن بدل تسليم سيرة شبه فارغة.
        logger.warning(
            "استجابة Gemini status=complete لكن بدون محتوى كافٍ بعد التنظيف "
            "(محاولة محتوى %s/%s, language=%s) - سنعيد المحاولة",
            content_attempt, max_content_retries, language,
        )

    raise RuntimeError("لم يتمكن Gemini من استخراج محتوى كافٍ للسيرة الذاتية بعد عدة محاولات")


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