"""
Бот «Что за письмо?» — израильские официальные письма по-русски.

Что умеет:
  • разбор письма по фото/PDF: кто, что хотят, суммы, сроки, план действий, риски, права
  • проверенные контакты ведомств из базы (модель их не генерирует)
  • автоматические напоминания о сроках: за 7 дней, за 2 дня, в день срока
  • детектор мошенничества
  • кнопка «Специалист» — сбор лида для партнёров (адвокат/бухгалтер/страховой)
  • кредиты, рефералка, оплата Telegram Stars, админка

Запуск:  python bot.py
"""
import asyncio
import logging
import re
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    LabeledPrice, Message, PreCheckoutQuery, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

import db
from analyzer import (SPECIALIST_RU, analyze_letter, format_contacts, format_glossary,
                      format_main, format_reply_draft)
from config import (ADMIN_IDS, BOT_TOKEN, BOT_USERNAME, FREE_LETTERS_ON_START, GEMINI_API_KEY,
                    LEAD_CHAT_ID, MAX_IMAGE_MB, MAX_PAGES, PACKAGES, REFERRAL_BONUS)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

router = Router()
_bot_username: str = BOT_USERNAME
_last_request: dict[int, float] = {}
RATE_LIMIT_SEC = 8


class LeadForm(StatesGroup):
    phone = State()
    note = State()


# ================================================================== клавиатуры
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Пригласить друга (+3 письма)", callback_data="invite")],
        [InlineKeyboardButton(text="⏰ Мои сроки", callback_data="deadlines"),
         InlineKeyboardButton(text="📊 Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="💳 Купить письма", callback_data="buy")],
    ])


def kb_after_result(letter_id: int, r: dict) -> InlineKeyboardMarkup:
    rows = []
    if r.get("needs_specialist"):
        st = SPECIALIST_RU.get(r.get("specialist_type"), "специалист")
        rows.append([InlineKeyboardButton(text=f"🧑‍⚖️ Связаться: {st}", callback_data=f"lead:{letter_id}")])
    else:
        rows.append([InlineKeyboardButton(text="🧑‍⚖️ Нужен специалист", callback_data=f"lead:{letter_id}")])
    if r.get("deadline") and (r.get("days_left") or 0) >= 0:
        rows.append([InlineKeyboardButton(text="✅ Уже сделал — не напоминать", callback_data=f"done:{letter_id}")])
    rows.append([InlineKeyboardButton(text="👍 Полезно", callback_data=f"fb:{letter_id}:1"),
                 InlineKeyboardButton(text="👎 Не помогло", callback_data=f"fb:{letter_id}:0")])
    rows.append([InlineKeyboardButton(text="📤 Поделиться ботом", callback_data="invite")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_buy() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{name} — {stars} ⭐", callback_data=f"pay:{code}")]
            for code, name, _, stars in PACKAGES]
    rows.append([InlineKeyboardButton(text="🎁 Или пригласи друга — бесплатно", callback_data="invite")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_no_credits() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Пригласить друга — +3 письма", callback_data="invite")],
        [InlineKeyboardButton(text="💳 Купить пакет", callback_data="buy")],
    ])


def kb_phone_request() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, keyboard=[
        [KeyboardButton(text="📱 Отправить мой номер", request_contact=True)],
        [KeyboardButton(text="Отмена")],
    ])


def invite_link(user_id: int) -> str:
    return f"https://t.me/{_bot_username}?start=ref{user_id}"


# ================================================================== /start /help
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    ref_id = None
    if command.args and command.args.startswith("ref") and command.args[3:].isdigit():
        ref_id = int(command.args[3:])

    u = message.from_user
    user, is_new = db.get_or_create_user(u.id, u.username, u.first_name, ref_id)

    if is_new and user["referred_by"]:
        try:
            await message.bot.send_message(
                user["referred_by"],
                f"🎉 По твоей ссылке пришёл новый человек! +{REFERRAL_BONUS} письма. "
                f"Баланс: {db.get_credits(user['referred_by'])}")
        except Exception:  # noqa: BLE001
            pass

    await message.answer(
        "👋 Привет! Я разбираю <b>израильские официальные письма по-русски</b>.\n\n"
        "Пришло что-то из Битуах Леуми, налоговой, банка, ирии, купат холим, суда или "
        "Хоцаа ле-Поаль — и непонятно, что хотят? <b>Сфотографируй и отправь мне.</b>\n\n"
        "За полминуты ты получишь:\n"
        "• от кого письмо и что именно требуют\n"
        "• суммы, номера дел и <b>срок, к которому надо успеть</b>\n"
        "• пошаговый план: что, когда и как сделать\n"
        "• чем грозит, если не реагировать, и какие есть варианты\n"
        "• <b>проверенные телефоны и сайты</b> нужного ведомства\n"
        "• проверку на мошенничество\n"
        "• черновик ответа на иврите, если он нужен\n\n"
        "⏰ А если есть срок — я <b>сам напомню</b> за неделю, за 2 дня и в день срока.\n\n"
        f"🎁 У тебя <b>{user['credits']}</b> бесплатных разбора. За каждого друга — ещё +{REFERRAL_BONUS}.\n\n"
        "📷 <b>Отправь фото письма.</b>",
        reply_markup=kb_main())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Как пользоваться</b>\n"
        "1. Сфотографируй письмо целиком при хорошем свете (или пришли PDF/скан).\n"
        "2. Отправь фото — одно письмо за раз. Многостраничное — по одной странице, начиная с первой.\n"
        "3. Получи разбор, контакты, план и напоминания.\n\n"
        "<b>Команды</b>\n"
        "/deadlines — мои сроки и напоминания\n"
        "/balance — сколько разборов осталось\n"
        "/invite — ссылка для друзей (+3 письма за каждого)\n"
        "/buy — купить пакет разборов\n\n"
        "🔒 Фото не сохраняются — только тип письма и срок (для напоминаний).\n"
        "⚖️ Бот не заменяет юриста. При судах и крупных суммах — кнопка «Специалист».",
        reply_markup=kb_main())


# ================================================================== баланс / инвайт
@router.message(Command("balance"))
async def cmd_balance(message: Message):
    await _send_balance(message.from_user.id, message)


@router.callback_query(F.data == "balance")
async def cb_balance(call: CallbackQuery):
    await _send_balance(call.from_user.id, call.message)
    await call.answer()


async def _send_balance(user_id: int, target: Message):
    refs = db.referral_count(user_id)
    await target.answer(
        f"📊 Осталось разборов: <b>{db.get_credits(user_id)}</b>\n"
        f"👥 Приглашено друзей: <b>{refs}</b> (+{refs * REFERRAL_BONUS} писем)", reply_markup=kb_main())


@router.message(Command("invite"))
async def cmd_invite(message: Message):
    await _send_invite(message.from_user.id, message)


@router.callback_query(F.data == "invite")
async def cb_invite(call: CallbackQuery):
    await _send_invite(call.from_user.id, call.message)
    await call.answer()


async def _send_invite(user_id: int, target: Message):
    link = invite_link(user_id)
    share_text = ("Бот объясняет письма из Битуах Леуми, банка, налоговой и ирии по-русски: "
                  "фоткаешь письмо — получаешь что делать, сроки и телефоны. И сам напоминает о сроках. Бесплатно:")
    share_url = f"https://t.me/share/url?url={link}&text={share_text}"
    await target.answer(
        f"🎁 <b>Твоя ссылка:</b>\n{link}\n\n"
        f"За каждого, кто перейдёт и запустит бота — тебе <b>+{REFERRAL_BONUS} письма</b>, "
        f"другу — бонус к стартовым {FREE_LETTERS_ON_START}.\n\n"
        "Кинь её в семейный чат или группу олим — там она реально нужна.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📤 Отправить друзьям", url=share_url)]]),
        disable_web_page_preview=True)


# ================================================================== сроки
@router.message(Command("deadlines"))
async def cmd_deadlines(message: Message):
    await _send_deadlines(message.from_user.id, message)


@router.callback_query(F.data == "deadlines")
async def cb_deadlines(call: CallbackQuery):
    await _send_deadlines(call.from_user.id, call.message)
    await call.answer()


async def _send_deadlines(user_id: int, target: Message):
    import datetime as dt
    rows = db.active_reminders(user_id)
    if not rows:
        await target.answer("⏰ Активных сроков нет. Пришли письмо — если в нём есть срок, я поставлю напоминания.")
        return
    today = dt.date.today()
    L = ["⏰ <b>Твои сроки:</b>"]
    kb_rows = []
    for r in rows:
        d = dt.date.fromisoformat(r["deadline"])
        left = (d - today).days
        L.append(f"\n• <b>{d.strftime('%d.%m.%Y')}</b> ({'сегодня' if left == 0 else f'через {left} дн.'}) — "
                 f"{r['title'] or 'письмо'}\n  {r['what'] or ''}")
        kb_rows.append([InlineKeyboardButton(text=f"✅ Сделано: {(r['title'] or 'письмо')[:30]}",
                                             callback_data=f"done:{r['letter_id']}")])
    await target.answer("\n".join(L), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data.startswith("done:"))
async def cb_done(call: CallbackQuery):
    letter_id = int(call.data.split(":")[1])
    n = db.cancel_reminders(call.from_user.id, letter_id)
    await call.answer("Отлично! Напоминания сняты 👍" if n else "Напоминаний по этому письму уже нет", show_alert=False)


# ================================================================== лиды (специалист)
@router.callback_query(F.data.startswith("lead:"))
async def cb_lead(call: CallbackQuery, state: FSMContext):
    letter_id = int(call.data.split(":")[1])
    letter = db.get_letter(letter_id)
    st = SPECIALIST_RU.get(letter["spec_type"] if letter else None, "русскоязычный специалист")
    await state.set_state(LeadForm.phone)
    await state.update_data(letter_id=letter_id, spec_type=letter["spec_type"] if letter else None)
    await call.message.answer(
        f"🧑‍⚖️ Передам твой запрос: <b>{st}</b>.\n\n"
        "Первая консультация — бесплатно, ни к чему не обязывает. "
        "Специалист получит только тип письма и сумму, без фото и личных данных.\n\n"
        "📱 Оставь номер для связи (кнопка ниже) или напиши его текстом. Или «Отмена».",
        reply_markup=kb_phone_request())
    await call.answer()


@router.message(LeadForm.phone, F.contact)
async def lead_phone_contact(message: Message, state: FSMContext):
    await _lead_got_phone(message, state, message.contact.phone_number)


@router.message(LeadForm.phone, F.text)
async def lead_phone_text(message: Message, state: FSMContext):
    if message.text.strip().lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("Ок, отменил.", reply_markup=ReplyKeyboardRemove())
        return
    digits = re.sub(r"\D", "", message.text)
    if len(digits) < 9:
        await message.answer("Не похоже на номер. Напиши в формате 05X-XXXXXXX или нажми кнопку.")
        return
    await _lead_got_phone(message, state, message.text.strip())


async def _lead_got_phone(message: Message, state: FSMContext, phone: str):
    await state.update_data(phone=phone)
    await state.set_state(LeadForm.note)
    await message.answer(
        "Принял. Одной фразой: что для тебя главное в этой ситуации? "
        "(например: «хочу рассрочку», «не согласен с долгом», «просто объяснить, что делать»). "
        "Или напиши «пропустить».", reply_markup=ReplyKeyboardRemove())


@router.message(LeadForm.note, F.text)
async def lead_note(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    note = None if message.text.strip().lower() in ("пропустить", "-", "skip") else message.text.strip()[:300]
    lead_id = db.create_lead(message.from_user.id, data.get("letter_id"), data.get("spec_type"), data["phone"], note)
    letter = db.get_letter(data["letter_id"]) if data.get("letter_id") else None

    await message.answer(
        "✅ Запрос передан. С тобой свяжутся в рабочее время, обычно в течение дня.\n\n"
        "Пока ждёшь — не пропусти срок из письма, напоминания остаются активными.", reply_markup=kb_main())

    # уведомление админу / партнёрскому чату
    st = SPECIALIST_RU.get(data.get("spec_type"), "не определён")
    text = (f"🧑‍⚖️ <b>Новый лид #{lead_id}</b>\n"
            f"Специалист: {st}\n"
            f"Ведомство: {letter['org_id'] if letter else '—'} · тип: {letter['letter_type'] if letter else '—'}\n"
            f"Сумма: {f'{letter['amount_ils']:,.0f} ₪' if letter and letter['amount_ils'] else '—'}\n"
            f"Срок: {letter['deadline'] if letter and letter['deadline'] else '—'}\n"
            f"Телефон: <code>{data['phone']}</code>\n"
            f"Комментарий: {note or '—'}\n"
            f"TG: @{message.from_user.username or '—'} (id {message.from_user.id})")
    targets = ([LEAD_CHAT_ID] if LEAD_CHAT_ID else []) + list(ADMIN_IDS)
    for t in dict.fromkeys(targets):
        try:
            await message.bot.send_message(t, text)
        except Exception:  # noqa: BLE001
            log.warning("lead notify failed for %s", t)


# ================================================================== оплата Stars
@router.message(Command("buy"))
async def cmd_buy(message: Message):
    await _send_buy(message)


@router.callback_query(F.data == "buy")
async def cb_buy(call: CallbackQuery):
    await _send_buy(call.message)
    await call.answer()


async def _send_buy(target: Message):
    await target.answer(
        "💳 <b>Пакеты разборов</b> (оплата звёздами Telegram прямо здесь, без карт и сайтов):\n\n"
        + "\n".join(f"• {name} — {stars} ⭐" for _, name, _, stars in PACKAGES)
        + "\n\nЗвёзды покупаются в Telegram в пару касаний.", reply_markup=kb_buy())


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery):
    code = call.data.split(":", 1)[1]
    pkg = next((p for p in PACKAGES if p[0] == code), None)
    if not pkg:
        await call.answer("Пакет не найден", show_alert=True)
        return
    _, name, credits, stars = pkg
    await call.message.answer_invoice(
        title=f"Пакет «{name}»", description=f"{credits} разборов писем. Не сгорают.",
        payload=f"pkg:{code}", currency="XTR", prices=[LabeledPrice(label=name, amount=stars)],
        provider_token="")
    await call.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message):
    sp = message.successful_payment
    code = sp.invoice_payload.split(":", 1)[1] if ":" in sp.invoice_payload else ""
    pkg = next((p for p in PACKAGES if p[0] == code), None)
    if not pkg:
        log.error("Unknown package: %s", sp.invoice_payload)
        return
    _, name, credits, stars = pkg
    db.add_credits(message.from_user.id, credits)
    db.log_payment(message.from_user.id, code, sp.total_amount, credits, sp.telegram_payment_charge_id)
    await message.answer(f"✅ Оплата прошла! +<b>{credits}</b> разборов. Баланс: <b>{db.get_credits(message.from_user.id)}</b>\n\n📷 Присылай письмо.")
    for admin in ADMIN_IDS:
        try:
            await message.bot.send_message(admin, f"💰 Покупка: {name} за {stars} ⭐ от id {message.from_user.id}")
        except Exception:  # noqa: BLE001
            pass


# ================================================================== разбор письма
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}

# Альбомы (несколько фото одним сообщением) приходят отдельными апдейтами с общим media_group_id.
# Копим их ~1.5 сек и разбираем как одно многостраничное письмо.
_albums: dict[str, dict] = {}
ALBUM_WAIT_SEC = 1.5


def _extract_file(message: Message) -> tuple[str, str, int] | None:
    if message.photo:
        return message.photo[-1].file_id, "image/jpeg", message.photo[-1].file_size or 0
    if message.document and message.document.mime_type in IMAGE_MIMES:
        return message.document.file_id, message.document.mime_type, message.document.file_size or 0
    return None


@router.message(F.photo | (F.document & F.document.mime_type.in_(IMAGE_MIMES)))
async def on_letter(message: Message, state: FSMContext):
    await state.clear()
    f = _extract_file(message)
    if not f:
        return
    file_id, mime, size = f
    if size > MAX_IMAGE_MB * 1024 * 1024:
        await message.answer(f"Файл больше {MAX_IMAGE_MB} МБ. Пришли фото поменьше.")
        return

    gid = message.media_group_id
    if not gid:
        await _process_letter(message, [(file_id, mime)])
        return

    # альбом: собираем страницы
    album = _albums.setdefault(gid, {"files": [], "message": message, "task": None})
    if len(album["files"]) < MAX_PAGES:
        album["files"].append((file_id, mime))
    if album["task"]:
        album["task"].cancel()
    album["task"] = asyncio.create_task(_flush_album(gid))


async def _flush_album(gid: str):
    await asyncio.sleep(ALBUM_WAIT_SEC)
    album = _albums.pop(gid, None)
    if album:
        await _process_letter(album["message"], album["files"])


async def _process_letter(message: Message, files: list[tuple[str, str]]):
    user = message.from_user
    db.get_or_create_user(user.id, user.username, user.first_name)

    now = time.time()
    if now - _last_request.get(user.id, 0) < RATE_LIMIT_SEC:
        await message.answer("⏳ Секунду, ещё разбираю предыдущее. Присылай по одному письму.")
        return
    _last_request[user.id] = now

    if not db.spend_credit(user.id):
        await message.answer("😕 Разборы закончились.\n\n"
                             f"Пригласи друга — +{REFERRAL_BONUS} письма бесплатно, или возьми пакет за звёзды.",
                             reply_markup=kb_no_credits())
        return

    pages_txt = f" ({len(files)} стр.)" if len(files) > 1 else ""
    status = await message.answer(f"🔍 Читаю письмо{pages_txt} и сверяю с базой… ~30 секунд.")
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        pages = []
        for file_id, mime in files:
            buf = await message.bot.download(file_id)
            pages.append((buf.read(), mime))
        result = await analyze_letter(pages)
    except Exception as e:  # noqa: BLE001
        log.exception("analyze failed")
        db.refund_credit(user.id)
        await status.edit_text("😔 Не получилось прочитать письмо. Разбор не списан.\n"
                               "Пересними при хорошем свете, целиком и без бликов.")
        for admin in ADMIN_IDS:
            try:
                await message.bot.send_message(admin, f"❗️ Ошибка разбора у {user.id}: {type(e).__name__}: {e}"[:500])
            except Exception:  # noqa: BLE001
                pass
        return

    if not result.get("is_document", True):
        db.refund_credit(user.id)
        await status.edit_text(format_main(result))
        return

    letter_id = db.log_letter(user.id, result)
    await status.delete()

    # 1) основной разбор
    await message.answer(format_main(result), disable_web_page_preview=True)
    # 2) контакты + кнопки
    await message.answer(format_contacts(result), reply_markup=kb_after_result(letter_id, result),
                         disable_web_page_preview=True)
    # 3) черновик ответа
    draft = format_reply_draft(result)
    if draft:
        await message.answer(draft)
    # 4) словарик
    gl = format_glossary(result)
    if gl:
        await message.answer(gl)

    # 5) напоминания
    if result.get("deadline") and (result.get("days_left") or -1) >= 0:
        n = db.schedule_reminders(user.id, letter_id, result["deadline"], result.get("deadline_what"), result.get("title"))
        if n:
            await message.answer(f"⏰ Поставил напоминания о сроке <b>{_ru_date(result['deadline'])}</b>: "
                                 "за 7 дней, за 2 дня и в день срока. Сделаешь раньше — нажми «Уже сделал».")

    credits_left = db.get_credits(user.id)
    if credits_left <= 1:
        await message.answer(f"ℹ️ Осталось разборов: <b>{credits_left}</b>. Поделись ботом с другом — получишь +{REFERRAL_BONUS}.",
                             reply_markup=kb_no_credits())


def _ru_date(iso: str) -> str:
    import datetime as dt
    try:
        return dt.date.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso


@router.callback_query(F.data.startswith("fb:"))
async def cb_feedback(call: CallbackQuery):
    _, letter_id, useful = call.data.split(":")
    db.log_feedback(call.from_user.id, int(letter_id), useful == "1")
    await call.answer("Спасибо! Это помогает делать бота лучше." if useful == "1"
                      else "Спасибо, учтём. Если фото было нечётким — пересними, разбор станет точнее.")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, state: FSMContext):
    if await state.get_state():
        return  # в процессе формы — обработают другие хэндлеры
    db.get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer("📷 Пришли <b>фото письма</b> (или PDF) — и я его разберу.\n"
                         "Сфотографируй документ целиком при хорошем свете.", reply_markup=kb_main())


# ================================================================== фоновые напоминания
async def reminder_loop(bot: Bot):
    """Каждую минуту проверяет, не пора ли отправить напоминание."""
    kind_text = {"d7": "через неделю", "d2": "через 2 дня", "d0": "СЕГОДНЯ"}
    while True:
        try:
            for r in db.due_reminders():
                txt = (f"⏰ <b>Напоминание: срок {kind_text.get(r['kind'], '')}</b> — {_ru_date(r['deadline'])}\n"
                       f"📄 {r['title'] or 'письмо'}\n"
                       f"Что нужно: {r['what'] or 'отреагировать на письмо'}\n\n"
                       + ("Если ещё не сделал — сегодня последний день. Не успеваешь — позвони в ведомство и попроси продление (הארכת מועד)."
                          if r["kind"] == "d0" else "Сделал — нажми кнопку, и я больше не буду напоминать."))
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Уже сделал", callback_data=f"done:{r['letter_id']}")],
                    [InlineKeyboardButton(text="🧑‍⚖️ Нужна помощь специалиста", callback_data=f"lead:{r['letter_id']}")]])
                try:
                    await bot.send_message(r["user_id"], txt, reply_markup=kb)
                except Exception as e:  # noqa: BLE001
                    log.warning("reminder send failed %s: %s", r["id"], e)
                db.mark_reminder_sent(r["id"])
        except Exception:  # noqa: BLE001
            log.exception("reminder loop error")
        await asyncio.sleep(60)


# ================================================================== админка
@router.message(Command("stats"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_stats(message: Message):
    s = db.stats()
    fb_total = s["useful_yes"] + s["useful_no"]
    fb_pct = f"{100 * s['useful_yes'] / fb_total:.0f}%" if fb_total else "—"
    top_orgs = "\n".join(f"  • {o}: {n}" for o, n in s["top_orgs"]) or "  —"
    top_types = "\n".join(f"  • {t}: {n}" for t, n in s["top_types"]) or "  —"
    await message.answer(
        "<b>📈 Статистика</b>\n"
        f"Пользователей: {s['users_total']} (+{s['users_24h']} сутки, +{s['users_7d']} неделя)\n"
        f"По рефералке: {s['users_via_referral']} · активных за неделю: {s['active_7d']}\n"
        f"Разборов: {s['letters_total']} (+{s['letters_24h']} за сутки)\n"
        f"Полезно: {fb_pct} ({fb_total} оценок)\n"
        f"Мошенничество выявлено: {s['scam_high']} · нужен специалист: {s['needs_spec']}\n"
        f"Активных напоминаний: {s['reminders_active']}\n"
        f"Лидов: {s['leads_total']} (новых: {s['leads_new']})\n"
        f"Платежей: {s['payments_total']} на {s['stars_total']} ⭐\n"
        f"Топ ведомств:\n{top_orgs}\nТоп типов писем:\n{top_types}")


@router.message(Command("leads"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_leads(message: Message, command: CommandObject):
    status = (command.args or "new").strip()
    rows = db.leads(None if status == "all" else status)
    if not rows:
        await message.answer(f"Лидов со статусом «{status}» нет.")
        return
    L = [f"<b>Лиды ({status}):</b>"]
    for r in rows:
        L.append(f"\n#{r['id']} · {SPECIALIST_RU.get(r['spec_type'], r['spec_type'] or '—')}\n"
                 f"{r['org_id'] or '—'} / {r['letter_type'] or '—'} · {f'{r['amount_ils']:,.0f} ₪' if r['amount_ils'] else '—'}\n"
                 f"📱 <code>{r['phone']}</code> · {r['note'] or ''}\n"
                 f"статус: {r['status']} → /lead_{r['id']}_sent · /lead_{r['id']}_closed")
    await message.answer("\n".join(L))


@router.message(F.text.regexp(r"^/lead_(\d+)_(sent|closed)$"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_lead_status(message: Message):
    m = re.match(r"^/lead_(\d+)_(sent|closed)$", message.text)
    db.set_lead_status(int(m.group(1)), m.group(2))
    await message.answer(f"Лид #{m.group(1)} → {m.group(2)}")


@router.message(Command("broadcast"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_broadcast(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Использование: /broadcast текст")
        return
    sent = failed = 0
    for uid in db.all_user_ids():
        try:
            await message.bot.send_message(uid, command.args)
            sent += 1
        except Exception:  # noqa: BLE001
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"Отправлено: {sent}, ошибок: {failed}")


@router.message(Command("give"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_give(message: Message, command: CommandObject):
    try:
        uid, n = command.args.split()
        db.add_credits(int(uid), int(n))
        await message.answer(f"Начислено {n} → {uid}. Баланс: {db.get_credits(int(uid))}")
    except Exception:  # noqa: BLE001
        await message.answer("Использование: /give <user_id> <кол-во>")


# ================================================================== запуск
async def main():
    global _bot_username
    if not BOT_TOKEN or not GEMINI_API_KEY:
        raise SystemExit("Заполни BOT_TOKEN и GEMINI_API_KEY в .env (см. .env.example)")

    db.init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if not _bot_username:
        _bot_username = (await bot.get_me()).username
    log.info("Bot @%s started", _bot_username)

    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(reminder_loop(bot))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
