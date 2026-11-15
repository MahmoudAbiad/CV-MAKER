"""
مولّد PDF احترافي وعالي التوافق باستخدام ReportLab
يعالج جذرياً:
1. مشكلة المربعات الفارغة (Tofu) في خط NotoSansArabic عبر نظام الخطوط المزدوجة (Dual-Font: خط عربي للحروف العربية + خط لاتيني للإنجليزية والإيميلات والرموز والأقواس).
2. مشكلة انعكاس ترتيب الأسطر (من الأسفل للأعلى) في الفقرات العربية الطويلة عبر تقسيم الأسطر منطقياً بحساب عرض الكلمات قبل تطبيق التشكيل وخوارزمية BiDi على كل سطر.
3. حماية أرقام الهواتف (+963...) والبريد الإلكتروني والروابط من الانعكاس داخل السياق العربي عبر عزل اتجاه LTR (LRE/PDF) وعلامات اتجاه RLM.
4. محاذاة النقاط التفصيلية (Bullets) في عمود مخصص على اليمين في العربية وعلى اليسار في الإنجليزية.
"""
from __future__ import annotations

import gc
import html
import io
import logging
import os
import re
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Table, TableStyle

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
_FONTS_DIR = _BASE_DIR / "assets" / "fonts"
logger = logging.getLogger(__name__)

_LABELS = {
    "ar": {
        "summary": "نبذة مهنية",
        "experience": "الخبرات العملية",
        "education": "التعليم والمؤهلات",
        "skills": "المهارات",
        "languages": "اللغات",
        "contact": "معلومات التواصل",
    },
    "en": {
        "summary": "Professional Summary",
        "experience": "Professional Experience",
        "education": "Education",
        "skills": "Skills",
        "languages": "Languages",
        "contact": "Contact",
    },
}

# جدول أشكال الحروف العربية القياسي (Presentation Forms-B: معزول، نهائي، ابتدائي، وسطي)
_ARABIC_FORMS: dict[str, tuple[str, ...]] = {
    "\u0621": ("\uFE80",),                                  # ء
    "\u0622": ("\uFE81", "\uFE82"),                         # آ
    "\u0623": ("\uFE83", "\uFE84"),                         # أ
    "\u0624": ("\uFE85", "\uFE86"),                         # ؤ
    "\u0625": ("\uFE87", "\uFE88"),                         # إ
    "\u0626": ("\uFE89", "\uFE8A", "\uFE8B", "\uFE8C"),     # ئ
    "\u0627": ("\uFE8D", "\uFE8E"),                         # ا
    "\u0628": ("\uFE8F", "\uFE90", "\uFE91", "\uFE92"),     # ب
    "\u0629": ("\uFE93", "\uFE94"),                         # ة
    "\u062A": ("\uFE95", "\uFE96", "\uFE97", "\uFE98"),     # ت
    "\u062B": ("\uFE99", "\uFE9A", "\uFE9B", "\uFE9C"),     # ث
    "\u062C": ("\uFE9D", "\uFE9E", "\uFE9F", "\uFEA0"),     # ج
    "\u062D": ("\uFEA1", "\uFEA2", "\uFEA3", "\uFEA4"),     # ح
    "\u062E": ("\uFEA5", "\uFEA6", "\uFEA7", "\uFEA8"),     # خ
    "\u062F": ("\uFEA9", "\uFEAA"),                         # د
    "\u0630": ("\uFEAB", "\uFEAC"),                         # ذ
    "\u0631": ("\uFEAD", "\uFEAE"),                         # ر
    "\u0632": ("\uFEAF", "\uFEB0"),                         # ز
    "\u0633": ("\uFEB1", "\uFEB2", "\uFEB3", "\uFEB4"),     # س
    "\u0634": ("\uFEB5", "\uFEB6", "\uFEB7", "\uFEB8"),     # ش
    "\u0635": ("\uFEB9", "\uFEBA", "\uFEBB", "\uFEBC"),     # ص
    "\u0636": ("\uFEBD", "\uFEBE", "\uFEBF", "\uFEC0"),     # ض
    "\u0637": ("\uFEC1", "\uFEC2", "\uFEC3", "\uFEC4"),     # ط
    "\u0638": ("\uFEC5", "\uFEC6", "\uFEC7", "\uFEC8"),     # ظ
    "\u0639": ("\uFEC9", "\uFECA", "\uFECB", "\uFECC"),     # ع
    "\u063A": ("\uFECD", "\uFECE", "\uFECF", "\uFED0"),     # غ
    "\u0641": ("\uFED1", "\uFED2", "\uFED3", "\uFED4"),     # ف
    "\u0642": ("\uFED5", "\uFED6", "\uFED7", "\uFED8"),     # ق
    "\u0643": ("\uFED9", "\uFEDA", "\uFEDB", "\uFEDC"),     # ك
    "\u0644": ("\uFEDD", "\uFEDE", "\uFEDF", "\uFEE0"),     # ل
    "\u0645": ("\uFEE1", "\uFEE2", "\uFEE3", "\uFEE4"),     # م
    "\u0646": ("\uFEE5", "\uFEE6", "\uFEE7", "\uFEE8"),     # ن
    "\u0647": ("\uFEE9", "\uFEEA", "\uFEEB", "\uFEEC"),     # ه
    "\u0648": ("\uFEED", "\uFEEE"),                         # و
    "\u0649": ("\uFEEF", "\uFEF0"),                         # ى
    "\u064A": ("\uFEF1", "\uFEF2", "\uFEF3", "\uFEF4"),     # ي
    "\u067E": ("\uFB56", "\uFB57", "\uFB58", "\uFB59"),     # پ
    "\u0686": ("\uFB7A", "\uFB7B", "\uFB7C", "\uFB7D"),     # چ
    "\u0698": ("\uFB8A", "\uFB8B"),                         # ژ
    "\u06A4": ("\uFB6A", "\uFB6B", "\uFB6C", "\uFB6D"),     # ڤ
    "\u06AF": ("\uFB92", "\uFB93", "\uFB94", "\uFB95"),     # گ
}

_LAM_ALEF_LIGATURES: dict[str, tuple[str, str]] = {
    "\u0622": ("\uFEF5", "\uFEF6"),  # لآ
    "\u0623": ("\uFEF7", "\uFEF8"),  # لأ
    "\u0625": ("\uFEF9", "\uFEFA"),  # لإ
    "\u0627": ("\uFEFB", "\uFEFC"),  # لا
}

_MIRROR_BRACKETS: dict[str, str] = {
    "(": ")",
    ")": "(",
    "[": "]",
    "]": "[",
    "{": "}",
    "}": "{",
    "<": ">",
    ">": "<",
    "«": "»",
    "»": "«",
}

_HARAKAT_RE = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_CONTROL_CHARS_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_LTR_TOKEN_RE = re.compile(
    r"(\+\d[\d\s\-]{5,}\d|[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+|https?://\S+)"
)

try:
    import arabic_reshaper as _ar_reshaper_mod

    _SHARED_RESHAPER = _ar_reshaper_mod.ArabicReshaper(
        configuration={
            "delete_harakat": True,
            "support_ligatures": False,
        }
    )
except Exception:
    _SHARED_RESHAPER = None

_cached_font_pair: tuple[str, str, str, str] | None = None


def _can_connect_after(ch: str) -> bool:
    if ch == "\u0640":
        return True
    forms = _ARABIC_FORMS.get(ch)
    return bool(forms and len(forms) == 4)


def _can_connect_before(ch: str) -> bool:
    if ch == "\u0640":
        return True
    forms = _ARABIC_FORMS.get(ch)
    return bool(forms and len(forms) >= 2)


def _builtin_arabic_reshape(text: str) -> str:
    """تشكيل الحروف العربية إلى Presentation Forms-B مع دعم ارتباطات لام-ألف القياسية فقط."""
    clean = _HARAKAT_RE.sub("", text)
    chars = list(clean)
    n = len(chars)
    out: list[str] = []
    i = 0
    while i < n:
        ch = chars[i]
        prev_ch = chars[i - 1] if i > 0 else ""
        connects_prev = bool(prev_ch and _can_connect_after(prev_ch))

        if ch == "\u0644" and i + 1 < n and chars[i + 1] in _LAM_ALEF_LIGATURES:
            iso_lig, fin_lig = _LAM_ALEF_LIGATURES[chars[i + 1]]
            out.append(fin_lig if connects_prev else iso_lig)
            i += 2
            continue

        forms = _ARABIC_FORMS.get(ch)
        if not forms:
            out.append(ch)
            i += 1
            continue

        next_ch = chars[i + 1] if i + 1 < n else ""
        connects_next = bool(next_ch and len(forms) == 4 and _can_connect_before(next_ch))

        if len(forms) == 1:
            out.append(forms[0])
        elif len(forms) == 2:
            out.append(forms[1] if connects_prev else forms[0])
        else:
            if connects_prev and connects_next:
                out.append(forms[3])
            elif connects_prev:
                out.append(forms[1])
            elif connects_next:
                out.append(forms[2])
            else:
                out.append(forms[0])
        i += 1
    return "".join(out)


def _pure_python_bidi(text: str, base_dir: str = "R") -> str:
    """بديل خفيف لمعالجة اتجاه BiDi وعكس الأقواس عند عدم توفر مكتبة python-bidi."""
    try:
        from fpdf.bidi import BidiParagraph, TextDirection

        direction = TextDirection.RTL if base_dir == "R" else TextDirection.LTR
        chars = BidiParagraph(text, base_direction=direction).get_reordered_characters()
        return "".join(
            _MIRROR_BRACKETS.get(c.character, c.character) if c.embedding_level % 2 == 1 else c.character
            for c in chars
        )
    except Exception:
        pass

    if base_dir != "R":
        return text

    token_re = re.compile(r"(\u202A[^\u202C]*\u202C|[A-Za-z0-9][A-Za-z0-9@._+\-/:&]*[A-Za-z0-9]|[A-Za-z0-9]+)")
    parts = token_re.split(text)
    visual_parts: list[str] = []
    for part in reversed(parts):
        if not part:
            continue
        if token_re.fullmatch(part):
            visual_parts.append(part)
        else:
            reversed_chars = [_MIRROR_BRACKETS.get(c, c) for c in reversed(part)]
            visual_parts.append("".join(reversed_chars))
    return "".join(visual_parts)


def _bidi_display(text: str, base_dir: str = "R") -> str:
    """يطبق خوارزمية Unicode BiDi مع تحديد اتجاه الفقرة الأساسي صراحةً."""
    try:
        from bidi.algorithm import get_display

        return get_display(text, base_dir=base_dir)
    except Exception:
        return _pure_python_bidi(text, base_dir=base_dir)


def _try_register_ttf(font_name: str, path: Path | str) -> bool:
    registered = set(pdfmetrics.getRegisteredFontNames())
    if font_name in registered:
        return True
    p = Path(path)
    if p.is_file():
        try:
            pdfmetrics.registerFont(TTFont(font_name, str(p)))
            return True
        except Exception:
            return False
    return False


def _get_font_suite() -> tuple[str, str, str, str]:
    """
    يسجل ويعيد أربعة خطوط: (arabic_regular, arabic_bold, latin_regular, latin_bold).
    يضمن عدم ظهور مربعات فارغة (Tofu) عند مزج العربية مع الإنجليزية والرموز.
    """
    global _cached_font_pair
    if _cached_font_pair is not None:
        return _cached_font_pair

    ar_reg_candidates = [
        ("NotoSansArabic", _FONTS_DIR / "NotoSansArabic-Regular.ttf"),
        ("NotoSansArabic", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
        ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("Arial", "C:/Windows/Fonts/arial.ttf"),
        ("SegoeUI", "C:/Windows/Fonts/segoeui.ttf"),
    ]
    ar_bold_candidates = [
        ("NotoSansArabic-Bold", _FONTS_DIR / "NotoSansArabic-Bold.ttf"),
        ("NotoSansArabic-Bold", "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"),
        ("DejaVuSans-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("Arial-Bold", "C:/Windows/Fonts/arialbd.ttf"),
        ("SegoeUI-Bold", "C:/Windows/Fonts/segoeuib.ttf"),
    ]
    lat_reg_candidates = [
        ("Cairo", _FONTS_DIR / "Cairo-Regular.ttf"),
        ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("LiberationSans", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        ("Arial", "C:/Windows/Fonts/arial.ttf"),
        ("SegoeUI", "C:/Windows/Fonts/segoeui.ttf"),
    ]
    lat_bold_candidates = [
        ("Cairo-Bold", _FONTS_DIR / "Cairo-Bold.ttf"),
        ("DejaVuSans-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("LiberationSans-Bold", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("Arial-Bold", "C:/Windows/Fonts/arialbd.ttf"),
        ("SegoeUI-Bold", "C:/Windows/Fonts/segoeuib.ttf"),
    ]

    ar_reg = "Helvetica"
    for name, path in ar_reg_candidates:
        if _try_register_ttf(name, path):
            ar_reg = name
            break

    ar_bold = ar_reg
    for name, path in ar_bold_candidates:
        if _try_register_ttf(name, path):
            ar_bold = name
            break

    lat_reg = "Helvetica"
    for name, path in lat_reg_candidates:
        if _try_register_ttf(name, path):
            lat_reg = name
            break

    lat_bold = "Helvetica-Bold" if lat_reg == "Helvetica" else lat_reg
    for name, path in lat_bold_candidates:
        if _try_register_ttf(name, path):
            lat_bold = name
            break

    _cached_font_pair = (ar_reg, ar_bold, lat_reg, lat_bold)
    return _cached_font_pair


def _get_registered_font() -> str:
    """للتوافق مع الاختبارات الحالية: يعيد اسم الخط العربي الأساسي المسجل."""
    return _get_font_suite()[0]


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _shape_arabic_only(text: str) -> str:
    """يشكّل الحروف العربية مع ضمان استخدام محارف Presentation Forms-B المدعومة في كافة الخطوط."""
    shaped = _builtin_arabic_reshape(text)
    if _SHARED_RESHAPER is not None and not _ARABIC_CHAR_RE.search(shaped):
        try:
            shaped = _SHARED_RESHAPER.reshape(_HARAKAT_RE.sub("", text))
        except Exception:
            pass
    return shaped


def _reshape_text(text: Any, is_arabic: bool) -> str:
    """
    يعيد تشكيل النص العربي ومعالجة اتجاه BiDi لسطر واحد بأمان وسرعة.
    يدعم أيضاً معالجة الكلمات العربية إن وردت داخل سيرة ذاتية إنجليزية.
    """
    s = _clean_str(text)
    if not s:
        return ""
    has_ar = bool(_ARABIC_CHAR_RE.search(s))
    if not is_arabic and not has_ar:
        return s

    protected = _LTR_TOKEN_RE.sub(lambda m: f"\u202A{m.group(1)}\u202C", s)
    reshaped = _shape_arabic_only(protected)
    base_dir = "R" if is_arabic else "L"
    visual = _bidi_display(reshaped, base_dir=base_dir)
    return _CONTROL_CHARS_RE.sub("", visual)


def _font_has_char(font_name: str, ch: str) -> bool:
    try:
        font_obj = pdfmetrics.getFont(font_name)
        cmap = getattr(getattr(font_obj, "face", None), "charToGlyph", None)
        if cmap is not None:
            return ord(ch) in cmap
    except Exception:
        pass
    return ord(ch) < 128


def _tag_dual_fonts(visual_text: str, ar_font: str, lat_font: str) -> str:
    """
    يفحص كل حرف في السطر المرئي:
    - الحروف العربية المدعومة في الخط العربي تُحاط بـ <font name="ar_font">
    - الحروف اللاتينية والأرقام والرموز والأقواس تُحاط بـ <font name="lat_font">
    مما يقضي نهائياً على مشكلة المربعات الفارغة (Tofu) في NotoSansArabic.
    """
    if not visual_text:
        return ""
    if ar_font == lat_font:
        return html.escape(visual_text)

    segments: list[tuple[str, list[str]]] = []
    for ch in visual_text:
        cp = ord(ch)
        is_ar_script = (
            0x0600 <= cp <= 0x06FF
            or 0x0750 <= cp <= 0x08FF
            or 0xFB50 <= cp <= 0xFDFF
            or 0xFE70 <= cp <= 0xFEFF
        )
        if is_ar_script and _font_has_char(ar_font, ch):
            target_font = ar_font
        elif _font_has_char(lat_font, ch):
            target_font = lat_font
        elif _font_has_char(ar_font, ch):
            target_font = ar_font
        else:
            target_font = "Helvetica"

        if segments and segments[-1][0] == target_font:
            segments[-1][1].append(ch)
        else:
            segments.append((target_font, [ch]))

    out_parts: list[str] = []
    for f_name, chars in segments:
        escaped = html.escape("".join(chars))
        out_parts.append(f'<font name="{f_name}">{escaped}</font>')
    return "".join(out_parts)


def _measure_word_width(word: str, font_size: float, ar_font: str, lat_font: str) -> float:
    clean_word = _CONTROL_CHARS_RE.sub("", word)
    if not clean_word:
        return 0.0
    shaped = _shape_arabic_only(clean_word) if _ARABIC_CHAR_RE.search(clean_word) else clean_word
    total_w = 0.0
    for ch in shaped:
        cp = ord(ch)
        is_ar_script = (
            0x0600 <= cp <= 0x06FF
            or 0x0750 <= cp <= 0x08FF
            or 0xFB50 <= cp <= 0xFDFF
            or 0xFE70 <= cp <= 0xFEFF
        )
        f_use = ar_font if (is_ar_script and _font_has_char(ar_font, ch)) else (
            lat_font if _font_has_char(lat_font, ch) else "Helvetica"
        )
        try:
            total_w += pdfmetrics.stringWidth(ch, f_use, font_size)
        except Exception:
            total_w += font_size * 0.55
    return total_w


def _format_paragraph_text(
    text: Any,
    is_arabic: bool,
    font_size: float,
    max_width_pt: float,
    ar_font: str,
    lat_font: str,
) -> str:
    """
    يقسّم الفقرة منطقياً إلى أسطر تتناسب مع العرض المتاح قبل تطبيق BiDi على كل سطر،
    مما يمنع انعكاس ترتيب الأسطر (من الأسفل للأعلى) في الفقرات العربية الطويلة داخل ReportLab.
    """
    s = _clean_str(text)
    if not s:
        return ""

    safe_max_w = max(max_width_pt * 0.95, 80.0)
    try:
        space_w = pdfmetrics.stringWidth(" ", lat_font, font_size)
    except Exception:
        space_w = font_size * 0.3

    logical_lines: list[str] = []
    for raw_para in s.splitlines():
        words = raw_para.split()
        if not words:
            continue
        current_words: list[str] = []
        current_w = 0.0
        for word in words:
            w = _measure_word_width(word, font_size, ar_font, lat_font)
            if current_words and (current_w + space_w + w > safe_max_w):
                logical_lines.append(" ".join(current_words))
                current_words = [word]
                current_w = w
            else:
                current_words.append(word)
                current_w = (current_w + space_w + w) if len(current_words) > 1 else w
        if current_words:
            logical_lines.append(" ".join(current_words))

    rendered_lines: list[str] = []
    for line in logical_lines:
        visual_line = _reshape_text(line, is_arabic)
        tagged_line = _tag_dual_fonts(visual_line, ar_font, lat_font)
        rendered_lines.append(tagged_line)

    return "<br/>".join(rendered_lines)


def generate_cv_pdf(cv_data: dict[str, Any]) -> bytes:
    """يولّد ملف PDF احترافي وعالي التوافق في الذاكرة دون كتابة ملفات مؤقتة."""
    language = _clean_str(cv_data.get("_language")) or "ar"
    is_arabic = language == "ar"
    labels = _LABELS.get(language, _LABELS["ar"])

    ar_reg, ar_bold, lat_reg, lat_bold = _get_font_suite()
    primary_font = ar_reg if is_arabic else lat_reg
    alignment = TA_RIGHT if is_arabic else TA_LEFT

    page_w, _ = A4
    margin_h = 15 * mm
    margin_v = 14 * mm
    usable_w = page_w - (2 * margin_h)

    def p_text(text: Any, font_size: float = 10.0, width_pt: float = usable_w, bold: bool = False) -> str:
        a_font = ar_bold if bold else ar_reg
        l_font = lat_bold if bold else lat_reg
        return _format_paragraph_text(text, is_arabic, font_size, width_pt, a_font, l_font)

    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=margin_h,
        leftMargin=margin_h,
        topMargin=margin_v,
        bottomMargin=margin_v,
    )

    styles = getSampleStyleSheet()

    base = ParagraphStyle(
        "Base",
        parent=styles["Normal"],
        fontName=primary_font,
        fontSize=10,
        leading=15,
        alignment=alignment,
        textColor=colors.HexColor("#2C3E50"),
    )
    name_style = ParagraphStyle(
        "Name",
        parent=base,
        fontSize=19,
        leading=25,
        textColor=colors.HexColor("#123A5E"),
        spaceAfter=3,
    )
    title_style = ParagraphStyle(
        "Title",
        parent=base,
        fontSize=12,
        leading=17,
        textColor=colors.HexColor("#2980B9"),
        spaceAfter=7,
    )
    contact_style = ParagraphStyle(
        "Contact",
        parent=base,
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor("#555555"),
        spaceAfter=9,
    )
    section_title_style = ParagraphStyle(
        "SectionTitle",
        parent=base,
        fontSize=12,
        leading=17,
        textColor=colors.HexColor("#123A5E"),
        spaceBefore=8,
        spaceAfter=2,
        keepWithNext=True,
    )
    exp_header_style = ParagraphStyle(
        "ExpHeader",
        parent=base,
        fontSize=10.5,
        leading=15,
        textColor=colors.HexColor("#123A5E"),
        spaceBefore=5,
        spaceAfter=2,
        keepWithNext=True,
    )
    bullet_text_style = ParagraphStyle(
        "BulletText",
        parent=base,
        fontSize=9.5,
        leading=14.5,
        spaceAfter=0,
    )
    bullet_dot_style = ParagraphStyle(
        "BulletDot",
        parent=base,
        fontName=lat_bold,
        fontSize=10,
        leading=14.5,
        alignment=TA_RIGHT if is_arabic else TA_LEFT,
        textColor=colors.HexColor("#123A5E"),
    )

    story: list[Any] = []
    item_sep = "\u200F  •  \u200F" if is_arabic else "  •  "

    # 1. الترويسة (الاسم الكامل والمسمى الوظيفي)
    full_name = _clean_str(cv_data.get("full_name"))
    if full_name:
        story.append(Paragraph(p_text(full_name, 19, bold=True), name_style))

    job_title = _clean_str(cv_data.get("title"))
    if job_title:
        story.append(Paragraph(p_text(job_title, 12, bold=True), title_style))

    # 2. معلومات الاتصال
    contact = cv_data.get("contact")
    if isinstance(contact, dict):
        parts = [
            _clean_str(contact.get("phone")),
            _clean_str(contact.get("email")),
            _clean_str(contact.get("location")),
        ]
        valid_parts = [p for p in parts if p]
        if valid_parts:
            contact_line = item_sep.join(valid_parts)
            story.append(Paragraph(p_text(contact_line, 9.5), contact_style))

    def add_section_header(title_text: str) -> None:
        story.append(Paragraph(p_text(title_text, 12, bold=True), section_title_style))
        story.append(
            HRFlowable(
                width="100%",
                thickness=1.2,
                color=colors.HexColor("#123A5E"),
                spaceBefore=1,
                spaceAfter=5,
            )
        )

    # 3. النبذة المهنية
    summary = _clean_str(cv_data.get("summary"))
    if summary:
        add_section_header(labels["summary"])
        story.append(Paragraph(p_text(summary, 10), base))

    # 4. الخبرات العملية
    experience = cv_data.get("experience")
    if isinstance(experience, list) and experience:
        valid_exp = [e for e in experience if isinstance(e, dict)]
        if valid_exp:
            add_section_header(labels["experience"])
            for exp in valid_exp:
                role = _clean_str(exp.get("role"))
                company = _clean_str(exp.get("company"))
                period = _clean_str(exp.get("period"))

                header_parts = []
                if role and company:
                    header_parts.append(f"{role} — {company}")
                elif role or company:
                    header_parts.append(role or company)

                if period:
                    header_parts.append(f"({period})")

                header_text = " ".join(header_parts)
                if header_text:
                    story.append(Paragraph(p_text(header_text, 10.5, bold=True), exp_header_style))

                bullets = exp.get("bullets")
                if isinstance(bullets, str):
                    bullets = [b.strip("-*• ") for b in bullets.splitlines() if b.strip("-*• ")]
                if isinstance(bullets, list):
                    dot_w = 4.5 * mm
                    txt_w = usable_w - dot_w
                    for b in bullets:
                        b_str = _clean_str(b)
                        if not b_str:
                            continue
                        b_para = Paragraph(p_text(b_str, 9.5, width_pt=txt_w), bullet_text_style)
                        dot_para = Paragraph(f'<font name="{lat_bold}">•</font>', bullet_dot_style)
                        if is_arabic:
                            row = [[b_para, dot_para]]
                            col_widths = [txt_w, dot_w]
                        else:
                            row = [[dot_para, b_para]]
                            col_widths = [dot_w, txt_w]

                        bullet_table = Table(row, colWidths=col_widths, splitByRow=1)
                        bullet_table.setStyle(
                            TableStyle(
                                [
                                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                                ]
                            )
                        )
                        story.append(bullet_table)

    # 5. التعليم والمؤهلات
    education = cv_data.get("education")
    if isinstance(education, list) and education:
        valid_edu = [e for e in education if isinstance(e, dict)]
        if valid_edu:
            add_section_header(labels["education"])
            for edu in valid_edu:
                degree = _clean_str(edu.get("degree"))
                inst = _clean_str(edu.get("institution"))
                period = _clean_str(edu.get("period"))

                edu_parts = []
                if degree and inst:
                    edu_parts.append(f"{degree} — {inst}")
                elif degree or inst:
                    edu_parts.append(degree or inst)

                if period:
                    edu_parts.append(f"({period})")

                edu_text = " ".join(edu_parts)
                if edu_text:
                    story.append(Paragraph(p_text(edu_text, 10), base))

    # 6. المهارات
    skills = cv_data.get("skills")
    if isinstance(skills, str):
        skills = [s.strip() for s in re.split(r"[,،\n]+", skills) if s.strip()]
    if isinstance(skills, list):
        clean_skills = [_clean_str(s) for s in skills if _clean_str(s)]
        if clean_skills:
            add_section_header(labels["skills"])
            story.append(Paragraph(p_text(item_sep.join(clean_skills), 10), base))

    # 7. اللغات
    languages = cv_data.get("languages")
    if isinstance(languages, str):
        languages = [l.strip() for l in re.split(r"[,،\n]+", languages) if l.strip()]
    if isinstance(languages, list):
        clean_langs = [_clean_str(l) for l in languages if _clean_str(l)]
        if clean_langs:
            add_section_header(labels["languages"])
            story.append(Paragraph(p_text(item_sep.join(clean_langs), 10), base))

    try:
        try:
            doc.build(story)
            pdf_bytes = buffer.getvalue()
            return pdf_bytes
        except Exception as exc:
            logger.warning(
                "تعذر إتمام تخطيط PDF الأساسي (%s)، الانتقال التلقائي للمولد الاحتياطي فائق الاعتمادية.",
                exc,
                exc_info=True,
            )
            return _build_fallback_pdf(cv_data, is_arabic, primary_font, lat_reg, labels)
    finally:
        buffer.close()
        del story
        del doc
        gc.collect(1)


def _build_fallback_pdf(
    cv_data: dict[str, Any],
    is_arabic: bool,
    primary_font: str,
    lat_font: str,
    labels: dict[str, str],
) -> bytes:
    """مولّد احتياطي مبسط ومضمون 100% يعمل بدون جداول معقدة في حال فشل التخطيط الأساسي."""
    buf = io.BytesIO()
    try:
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=14 * mm,
            rightMargin=14 * mm,
            topMargin=12 * mm,
            bottomMargin=12 * mm,
        )
        styles = getSampleStyleSheet()
        align = TA_RIGHT if is_arabic else TA_LEFT

        name_style = ParagraphStyle(
            "FallbackName",
            parent=styles["Normal"],
            fontName=primary_font,
            fontSize=16,
            leading=22,
            alignment=align,
            textColor=colors.HexColor("#123A5E"),
            spaceAfter=4,
        )
        title_style = ParagraphStyle(
            "FallbackTitle",
            parent=styles["Normal"],
            fontName=primary_font,
            fontSize=11,
            leading=16,
            alignment=align,
            textColor=colors.HexColor("#374151"),
            spaceAfter=6,
        )
        sec_style = ParagraphStyle(
            "FallbackSec",
            parent=styles["Normal"],
            fontName=primary_font,
            fontSize=12,
            leading=18,
            alignment=align,
            textColor=colors.HexColor("#123A5E"),
            spaceBefore=8,
            spaceAfter=3,
        )
        body_style = ParagraphStyle(
            "FallbackBody",
            parent=styles["Normal"],
            fontName=primary_font,
            fontSize=9.5,
            leading=14,
            alignment=align,
            textColor=colors.HexColor("#1F2937"),
            spaceAfter=2,
        )

        def fb_text(txt: Any) -> str:
            raw = _clean_str(txt)
            if not raw:
                return ""
            visual = _reshape_text(raw, is_arabic)
            return _tag_dual_fonts(visual, primary_font, lat_font)

        story: list[Any] = []
        name = _clean_str(cv_data.get("full_name") or ("السيرة الذاتية" if is_arabic else "Curriculum Vitae"))
        story.append(Paragraph(fb_text(name), name_style))

        title = _clean_str(cv_data.get("title"))
        if title:
            story.append(Paragraph(fb_text(title), title_style))

        contact = cv_data.get("contact")
        if isinstance(contact, dict):
            c_parts = [_clean_str(contact.get(k)) for k in ("phone", "email", "location") if _clean_str(contact.get(k))]
            if c_parts:
                sep = "  •  "
                story.append(Paragraph(fb_text(sep.join(c_parts)), body_style))

        def add_fb_sec(h_text: str) -> None:
            story.append(Paragraph(fb_text(h_text), sec_style))
            story.append(
                HRFlowable(
                    width="100%",
                    thickness=1,
                    color=colors.HexColor("#123A5E"),
                    spaceBefore=1,
                    spaceAfter=4,
                )
            )

        summary = _clean_str(cv_data.get("summary"))
        if summary:
            add_fb_sec(labels.get("summary", "النبذة المهنية"))
            story.append(Paragraph(fb_text(summary), body_style))

        experience = cv_data.get("experience")
        if isinstance(experience, list) and experience:
            add_fb_sec(labels.get("experience", "الخبرات العملية"))
            for exp in experience:
                if isinstance(exp, dict):
                    role = _clean_str(exp.get("role"))
                    company = _clean_str(exp.get("company"))
                    period = _clean_str(exp.get("period"))
                    h_parts = [p for p in (role, company) if p]
                    h = " — ".join(h_parts)
                    if period:
                        h = f"{h} ({period})" if h else f"({period})"
                    if h:
                        story.append(Paragraph(fb_text(h), body_style))
                    bullets = exp.get("bullets")
                    if isinstance(bullets, str):
                        bullets = [b.strip("-*• ") for b in bullets.splitlines() if b.strip("-*• ")]
                    if isinstance(bullets, list):
                        for b in bullets:
                            b_str = _clean_str(b)
                            if b_str:
                                story.append(Paragraph(fb_text(f"• {b_str}"), body_style))

        education = cv_data.get("education")
        if isinstance(education, list) and education:
            add_fb_sec(labels.get("education", "التعليم"))
            for edu in education:
                if isinstance(edu, dict):
                    degree = _clean_str(edu.get("degree"))
                    inst = _clean_str(edu.get("institution"))
                    period = _clean_str(edu.get("period"))
                    h_parts = [p for p in (degree, inst) if p]
                    h = " — ".join(h_parts)
                    if period:
                        h = f"{h} ({period})" if h else f"({period})"
                    if h:
                        story.append(Paragraph(fb_text(h), body_style))

        skills = cv_data.get("skills")
        if isinstance(skills, str):
            skills = [s.strip() for s in re.split(r"[,،\n]+", skills) if s.strip()]
        if isinstance(skills, list):
            s_clean = [_clean_str(s) for s in skills if _clean_str(s)]
            if s_clean:
                add_fb_sec(labels.get("skills", "المهارات"))
                story.append(Paragraph(fb_text("  •  ".join(s_clean)), body_style))

        languages = cv_data.get("languages")
        if isinstance(languages, str):
            languages = [l.strip() for l in re.split(r"[,،\n]+", languages) if l.strip()]
        if isinstance(languages, list):
            l_clean = [_clean_str(l) for l in languages if _clean_str(l)]
            if l_clean:
                add_fb_sec(labels.get("languages", "اللغات"))
                story.append(Paragraph(fb_text("  •  ".join(l_clean)), body_style))

        try:
            doc.build(story)
            return buf.getvalue()
        except Exception:
            # آخر خط دفاعي: استخدام تخطيط فائق البساطة بخط قياسي
            buf.seek(0)
            buf.truncate(0)
            doc_minimal = SimpleDocTemplate(buf, pagesize=A4)
            minimal_story = [
                Paragraph(html.escape(name), styles["Heading1"]),
                Paragraph(html.escape(_clean_str(cv_data.get("summary") or "")), styles["Normal"]),
            ]
            doc_minimal.build(minimal_story)
            return buf.getvalue()
    finally:
        buf.close()

