"""
Разбор письма через Gemini (vision) + наложение проверенной базы знаний.

Принцип разделения ответственности:
  • Модель — читает иврит, классифицирует, вытаскивает факты, суммы, даты, оценивает риск
    мошенничества, пишет план и черновик ответа. Ей ЗАПРЕЩЕНО писать телефоны и ссылки.
  • Код  — подставляет проверенные контакты из knowledge.py, считает сроки по закону,
    вычищает любые «просочившиеся» номера/URL из текста модели.
"""
import asyncio
import datetime as dt
import json
import logging
import re

from google import genai
from google.genai import types

import knowledge as kb
from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_THINKING_BUDGET

log = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# --------------------------------------------------------------------------- промпт
def build_system_prompt(today: dt.date) -> str:
    org_list = "\n".join(f"  {k} — {v['name_ru']} ({v['name_he']})" for k, v in kb.ORGS.items())
    type_list = "\n".join(f"  {k} — {v}" for k, v in kb.LETTER_TYPES.items())
    scam_list = "\n".join(f"  - {s}" for s in kb.SCAM_PATTERNS)

    return f"""Ты — опытный русскоязычный консультант по израильской бюрократии. Ты 15 лет помогаешь репатриантам
разбираться с письмами от ведомств, банков, судов и компаний. Ты объясняешь просто, по делу, без канцелярита,
как объяснил бы другу за чашкой кофе — но точно и осторожно, потому что от твоих слов зависят деньги и сроки людей.

Сегодня: {today.isoformat()} ({today.strftime('%d.%m.%Y')}). Используй эту дату для расчёта сроков.

Тебе присылают фото или скан письма (иврит, иногда английский/арабский/русский). Твоя работа — разобрать его
и вернуть СТРОГО валидный JSON (без markdown-обёртки) по схеме ниже.

=== ЖЁСТКИЕ ПРАВИЛА ===
1. НИКОГДА не пиши телефоны, номера со звёздочкой (*6050 и т.п.), адреса сайтов, e-mail, названия приложений
   для скачивания. Контакты подставит система из проверенной базы. Если хочешь сказать «позвони в ведомство» —
   пиши именно так: «позвони в Битуах Леуми» без номера. Нарушение этого правила — худшая ошибка.
2. Не выдумывай. Суммы, даты, номера дел — только те, что реально видны в письме. Если не читается —
   confidence=low и напиши об этом в warnings.
3. Не давай юридических гарантий. При суде, Хоцаа ле-Поаль, суммах от ~5 000 ₪, отказе в статусе, увольнении —
   needs_specialist=true и объясни зачем.
4. Пиши по-русски. Ивритские термины давай в скобках на иврите при первом упоминании: «возражение (השגה)».
   Не транслитерируй иврит латиницей.
5. Обращайся на «ты», коротко, конкретно. Каждый пункт actions — одно действие, начинающееся с глагола.
6. Если это реклама, спам или явно не документ — так и скажи, не раздувай.

=== КЛАССИФИКАЦИЯ ===
org_id — выбери ОДИН из списка (кто отправитель):
{org_list}

letter_type — выбери ОДИН из списка (что это за письмо):
{type_list}

=== СПРАВОЧНИК ПРОЦЕДУР И СРОКОВ (проверенный, опирайся на него) ===
{kb.procedures_for_prompt()}

=== ПРИЗНАКИ МОШЕННИЧЕСТВА (проверяй каждое письмо) ===
{scam_list}
Если совпадает хотя бы один серьёзный признак — scam_risk=high и первым пунктом actions: «Ничего не оплачивай
и не вводи данные, пока не проверишь через официальный канал». Официальные письма ведомств содержат номер дела /
теудат зеут получателя / логотип и приходят почтой или в личный кабинет на gov.il.

=== СРОКИ ===
- deadline_date — заполняй ТОЛЬКО если в письме явно указана дата «до …» или срок в днях от даты письма
  (тогда посчитай: дата письма + дни; если даты письма нет — от сегодня и отметь deadline_basis="estimated").
- deadline_basis: "explicit" (дата прямо в письме) | "computed" (посчитана от даты письма) |
  "estimated" (посчитана от сегодня, т.к. даты письма нет) | null.
- Если в письме срока нет, но по справочнику есть законный срок на реакцию — НЕ ставь его в deadline_date,
  система посчитает сама. Упомяни его словами в plan.

=== СХЕМА JSON ===
{{
  "is_document": true,
  "confidence": "high|medium|low",
  "language": "he|en|ru|ar|mixed",
  "org_id": "один из списка",
  "org_name_detected": "как отправитель назван в самом письме (по-русски, кратко)",
  "letter_type": "один из списка",
  "urgency": "high|medium|low|none",
  "title": "суть письма одной фразой, до 70 знаков",
  "summary": "2–4 предложения простыми словами: что это, почему пришло, что от тебя хотят",
  "amount_ils": число или null,
  "case_number": "номер дела/счёта/обращения из письма или null",
  "letter_date": "YYYY-MM-DD или null",
  "deadline_date": "YYYY-MM-DD или null",
  "deadline_basis": "explicit|computed|estimated|null",
  "deadline_what": "что нужно сделать к этому сроку, коротко, или null",
  "key_facts": ["факт с цифрой/датой/номером — каждый отдельно, 2–7 штук"],
  "plan": [
    {{"step": "Что сделать (с глагола)", "when": "сегодня|на этой неделе|до <дата>|после ответа", "how": "как именно: онлайн в личном кабинете / по телефону в <ведомство> / лично в отделении / письмом — БЕЗ номеров и ссылок", "why": "зачем, одна фраза или null"}}
  ],
  "if_ignored": "что реально произойдёт, если ничего не делать — конкретно, без запугивания",
  "options": ["альтернативные пути, если есть: рассрочка, возражение, скидка, списание — каждый отдельно"],
  "rights_hint": "какое право/льгота может быть у человека в этой ситуации, о которой он может не знать (особенно для новых репатриантов), или null",
  "scam_risk": "none|low|high",
  "scam_reason": "почему, если low/high, иначе null",
  "needs_specialist": true/false,
  "specialist_type": "lawyer_debt|lawyer_btl|lawyer_immigration|lawyer_labor|accountant|insurance_agent|none",
  "specialist_reason": "зачем нужен специалист, одна фраза, или null",
  "questions_for_user": ["что ты не смог понять из фото и что стоит уточнить — 0–2 вопроса"],
  "reply_draft_he": "если уместен письменный ответ/возражение/запрос — короткий вежливый черновик на иврите 3–7 строк с плейсхолдерами [שם מלא], [ת.ז.], [מספר תיק]; иначе null",
  "reply_draft_ru": "перевод черновика на русский или null",
  "glossary": [{{"he": "термин на иврите из письма", "ru": "что это значит"}}]
}}
"""


# --------------------------------------------------------------------------- вызов модели
async def analyze_letter(image_bytes: bytes | list[tuple[bytes, str]], mime_type: str = "image/jpeg",
                         today: dt.date | None = None) -> dict:
    """
    image_bytes — либо одно изображение (bytes) + mime_type, либо список [(bytes, mime), ...]
    для многостраничного письма (страницы в порядке следования).
    """
    today = today or dt.date.today()
    client = _get_client()

    pages = image_bytes if isinstance(image_bytes, list) else [(image_bytes, mime_type)]
    parts = [types.Part.from_bytes(data=b, mime_type=m) for b, m in pages]
    hint = ("Разбери это письмо по схеме." if len(pages) == 1
            else f"Это одно письмо на {len(pages)} страницах, по порядку. Разбери его целиком по схеме.")

    cfg = dict(
        system_instruction=build_system_prompt(today),
        temperature=0.15,
        response_mime_type="application/json",
        max_output_tokens=6144,
    )
    if GEMINI_THINKING_BUDGET and GEMINI_THINKING_BUDGET > 0:
        cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET)

    async def _call(config):
        return await asyncio.to_thread(
            client.models.generate_content, model=GEMINI_MODEL, contents=parts + [hint], config=config
        )

    try:
        response = await _call(types.GenerateContentConfig(**cfg))
    except Exception as e:  # noqa: BLE001
        # модели без поддержки thinking_config — повторяем без него
        if "thinking" in str(e).lower() and "thinking_config" in cfg:
            cfg.pop("thinking_config")
            response = await _call(types.GenerateContentConfig(**cfg))
        else:
            raise

    raw = _parse_json(response.text or "")
    return enrich(raw, today)


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


# --------------------------------------------------------------------------- пост-обработка
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(
    r"(https?://\S+|www\.\S+|(?<![\w@.])(?:[\w-]+\.)*(?:gov\.il|co\.il|org\.il|ac\.il|muni\.il|[\w-]+\.(?:com|net|org|io))(?:/\S*)?)",
    re.IGNORECASE)
# Имена официальных порталов, которые модель может упоминать как название (без пути) — они верные и полезные
_URL_WHITELIST = {"gov.il", "btl.gov.il", "kolzchut.org.il"}


def _url_sub(m: re.Match) -> str:
    tok = m.group(0).rstrip(".,;:)")
    return tok if tok.lower() in _URL_WHITELIST else PLACEHOLDER
_PHONE_RE = re.compile(
    r"(\*\s?\d{3,5}\b"                       # *6050
    r"|\b1-?[578]00-?\d{2,3}-?\d{3,4}\b"     # 1-800-200-103
    r"|\b1-?599-?\d{3}-?\d{3}\b"             # 1-599-500-171
    r"|\b0\d{1,2}-?\d{3}-?\d{4}\b"           # 03-9733333 / 050-1234567 / 073-2055000
    r"|\b0\d{8,9}\b"                         # 039733333
    r"|\b(?:1299|118|103|106|110|100|171)\b)")  # короткие номера

PLACEHOLDER = "[см. контакты ниже]"


def _scrub(s):
    """Вычищает любые контакты, которые модель всё-таки написала. Порядок важен: email → url → phone."""
    if not isinstance(s, str):
        return s
    s = _EMAIL_RE.sub(PLACEHOLDER, s)
    s = _URL_RE.sub(_url_sub, s)
    s = _PHONE_RE.sub(PLACEHOLDER, s)
    # схлопываем «[см. контакты ниже] или [см. контакты ниже]»
    s = re.sub(rf"(\[см\. контакты ниже\])(\s*(?:или|,|/|·)\s*\[см\. контакты ниже\])+", r"\1", s)
    return s


def _scrub_deep(obj):
    if isinstance(obj, str):
        return _scrub(obj)
    if isinstance(obj, list):
        return [_scrub_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub_deep(v) for k, v in obj.items()}
    return obj


def _parse_date(s) -> dt.date | None:
    if not s or not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def enrich(r: dict, today: dt.date) -> dict:
    """Нормализует ответ модели, вычищает контакты, добавляет данные из базы знаний."""
    r = _scrub_deep(r)

    org_id = r.get("org_id") if r.get("org_id") in kb.ORGS else "other"
    letter_type = r.get("letter_type") if r.get("letter_type") in kb.LETTER_TYPES else "other"
    r["org_id"], r["letter_type"] = org_id, letter_type

    # --- сроки
    deadline = _parse_date(r.get("deadline_date"))
    basis = r.get("deadline_basis")
    letter_date = _parse_date(r.get("letter_date"))
    statutory = kb.statutory_days(org_id, letter_type)

    if deadline is None and statutory:
        # закон даёт N дней; считаем от даты письма (или от сегодня, если даты нет)
        anchor = letter_date or today
        deadline = anchor + dt.timedelta(days=statutory)
        basis = "statutory_from_letter" if letter_date else "statutory_from_today"
        if not r.get("deadline_what"):
            r["deadline_what"] = "отреагировать (законный срок)"

    r["deadline"] = deadline.isoformat() if deadline else None
    r["deadline_basis"] = basis if deadline else None
    r["days_left"] = (deadline - today).days if deadline else None
    r["statutory_days"] = statutory

    # --- контакты и предупреждения из базы
    r["contacts"] = kb.contacts_block(org_id, letter_type)
    r["org_scam_note"] = kb.scam_note(org_id)
    r["org_name_ru"] = kb.ORGS[org_id]["name_ru"] if org_id != "other" else (r.get("org_name_detected") or "неизвестно")
    r["letter_type_ru"] = kb.LETTER_TYPES[letter_type]

    # --- срочность: подтягиваем, если срок горит
    if r["days_left"] is not None and r["days_left"] <= 7 and r.get("urgency") in (None, "low", "medium"):
        r["urgency"] = "high"
    if r.get("scam_risk") == "high":
        r["urgency"] = "high"
    return r


# --------------------------------------------------------------------------- форматирование
URGENCY = {
    "high": ("🔴", "срочно"),
    "medium": ("🟠", "важно, но не горит"),
    "low": ("🟢", "не срочно"),
    "none": ("⚪️", "можно не реагировать"),
}
BASIS_TEXT = {
    "explicit": "дата указана в письме",
    "computed": "посчитано от даты письма",
    "estimated": "примерно, даты письма не видно",
    "statutory_from_letter": "законный срок, отсчёт от даты письма",
    "statutory_from_today": "законный срок, отсчёт от сегодня — уточни дату получения",
}
SPECIALIST_RU = {
    "lawyer_debt": "адвокат по долгам и Хоцаа ле-Поаль",
    "lawyer_btl": "адвокат по Битуах Леуми",
    "lawyer_immigration": "адвокат по статусу и МВД",
    "lawyer_labor": "адвокат по трудовому праву",
    "accountant": "бухгалтер / налоговый консультант",
    "insurance_agent": "страховой / пенсионный консультант",
}


def _e(s) -> str:
    return "" if s is None else str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_date(iso: str | None) -> str:
    d = _parse_date(iso)
    return d.strftime("%d.%m.%Y") if d else ""


def format_main(r: dict) -> str:
    """Основное сообщение: что это, факты, срок, план, риски."""
    if not r.get("is_document", True):
        return ("🤔 На фото не видно письма или документа.\n\n"
                "Сфотографируй письмо целиком при хорошем свете, чтобы текст читался.")

    icon, urg_txt = URGENCY.get(r.get("urgency", "medium"), URGENCY["medium"])
    L = [f"{icon} <b>{_e(r.get('title') or 'Разбор письма')}</b>",
         f"📨 От: <b>{_e(r['org_name_ru'])}</b>",
         f"📄 Тип: {_e(r['letter_type_ru'])} · {urg_txt}"]

    if r.get("scam_risk") == "high":
        L += ["", "🚨 <b>ПОХОЖЕ НА МОШЕННИЧЕСТВО</b>",
              f"{_e(r.get('scam_reason'))}",
              "Ничего не оплачивай и не вводи данные карты, пока не проверишь через официальный канал (контакты ниже)."]
    elif r.get("scam_risk") == "low":
        L += ["", f"⚠️ Есть сомнения в подлинности: {_e(r.get('scam_reason'))}"]

    L += ["", _e(r.get("summary", ""))]

    facts = r.get("key_facts") or []
    if r.get("amount_ils"):
        try:
            facts = [f"Сумма: {float(r['amount_ils']):,.0f} ₪".replace(",", " ")] + \
                    [f for f in facts if "₪" not in f and "сумм" not in f.lower()]
        except (TypeError, ValueError):
            pass
    if r.get("case_number"):
        facts.append(f"Номер дела/обращения: {r['case_number']}")
    if facts:
        L += ["", "<b>📌 Главное:</b>"] + [f"• {_e(f)}" for f in facts[:8]]

    if r.get("deadline"):
        dl = r["days_left"]
        if dl is None:
            when = ""
        elif dl < 0:
            when = f" — <b>срок прошёл {abs(dl)} дн. назад</b>"
        elif dl == 0:
            when = " — <b>СЕГОДНЯ</b>"
        else:
            when = f" — осталось <b>{dl} дн.</b>"
        L += ["", f"⏰ <b>Срок: до {_fmt_date(r['deadline'])}</b>{when}",
              f"<i>{_e(r.get('deadline_what') or '')} · {BASIS_TEXT.get(r.get('deadline_basis'), '')}</i>".strip(" ·")]
        if dl is not None and dl < 0:
            L.append("Срок прошёл — это не всегда конец: часто можно просить продление (הארכת מועד) с объяснением причины. Действуй сегодня.")

    plan = r.get("plan") or []
    if plan:
        L += ["", "<b>✅ План действий:</b>"]
        for i, p in enumerate(plan[:7], 1):
            if isinstance(p, str):
                L.append(f"{i}. {_e(p)}")
                continue
            line = f"{i}. <b>{_e(p.get('step'))}</b>"
            if p.get("when"):
                line += f" — <i>{_e(p['when'])}</i>"
            L.append(line)
            if p.get("how"):
                L.append(f"    ↳ {_e(p['how'])}")

    if r.get("options"):
        L += ["", "<b>🔀 Варианты:</b>"] + [f"• {_e(o)}" for o in r["options"][:5]]

    if r.get("if_ignored"):
        L += ["", f"<b>❗️ Если ничего не делать:</b> {_e(r['if_ignored'])}"]

    if r.get("rights_hint"):
        L += ["", f"💡 <b>Полезно знать:</b> {_e(r['rights_hint'])}"]

    if r.get("questions_for_user"):
        L += ["", "<b>❓ Уточни, чтобы разбор был точнее:</b>"] + [f"• {_e(q)}" for q in r["questions_for_user"][:2]]

    if r.get("confidence") == "low":
        L += ["", "<i>Фото читается плохо — часть данных могла распознаться неточно. Пересними при хорошем свете.</i>"]

    return "\n".join(L)


def format_contacts(r: dict) -> str:
    """Второе сообщение: проверенные контакты из базы + предупреждение о мошенничестве."""
    L = ["<b>📞 Куда обращаться (проверенные контакты):</b>"]
    for c in r.get("contacts", []):
        if c["kind"] == "org":
            L.append(f"\n<b>{_e(c['name'])}</b>")
            if c.get("phone"):
                L.append(f"☎️ {_e(c['phone'])}" + (f" · {_e(c['hours'])}" if c.get("hours") and c["hours"] != "—" else ""))
            if c.get("languages") and c["languages"] != "—":
                L.append(f"🗣 {_e(c['languages'])}")
            if c.get("online"):
                L.append(f"💻 {_e(c['online'])}")
            if c.get("site"):
                L.append(f"🔗 {c['site']}")
        else:
            L.append(f"\n<b>{_e(c['name'])}</b>")
            if c.get("phone"):
                L.append(f"☎️ {_e(c['phone'])}")
            if c.get("site"):
                L.append(f"🔗 {c['site']}")
            if c.get("note"):
                L.append(f"<i>{_e(c['note'])}</i>")

    if r.get("org_scam_note"):
        L += ["", f"🛡 {_e(r['org_scam_note'])}"]

    if r.get("needs_specialist"):
        st = SPECIALIST_RU.get(r.get("specialist_type"), "специалист")
        L += ["", f"🧑‍⚖️ <b>Здесь стоит подключить специалиста</b> ({_e(st)}): {_e(r.get('specialist_reason') or '')}",
              "Нажми кнопку ниже — передадим твой запрос проверенному русскоязычному специалисту, первая консультация бесплатно."]

    L += ["", "<i>Это разбор письма, а не юридическая консультация. Контакты проверены по официальным источникам, "
              "но часы работы могут меняться.</i>"]
    return "\n".join(L)


def format_reply_draft(r: dict) -> str | None:
    he = r.get("reply_draft_he")
    if not he:
        return None
    ru = r.get("reply_draft_ru") or ""
    return ("✍️ <b>Черновик ответа на иврите</b> — скопируй, подставь свои данные вместо [скобок]:\n\n"
            f"<code>{_e(he)}</code>\n\n<b>Что здесь написано:</b>\n<i>{_e(ru)}</i>")


def format_glossary(r: dict) -> str | None:
    g = [x for x in (r.get("glossary") or []) if isinstance(x, dict) and x.get("he") and x.get("ru")]
    if not g:
        return None
    return "📖 <b>Словарик из письма:</b>\n" + "\n".join(f"• <b>{_e(x['he'])}</b> — {_e(x['ru'])}" for x in g[:8])
