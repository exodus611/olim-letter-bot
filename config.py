"""Настройки бота. Все секреты берутся из .env (см. .env.example)."""
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Модель Gemini: Flash-модели доступны на бесплатном тарифе.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Бюджет «размышлений» модели (только для 2.5/3.x). 0 — быстрее и дешевле, 1024 — точнее на датах и суммах.
GEMINI_THINKING_BUDGET = int(os.getenv("GEMINI_THINKING_BUDGET", "1024"))

# Telegram user_id администраторов через запятую, например "123456,987654"
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

# Юзернейм бота без @ (нужен для реферальных ссылок). Если пусто — подтянем из API.
BOT_USERNAME = os.getenv("BOT_USERNAME", "")

DB_PATH = os.getenv("DB_PATH", "bot.db")

# Куда слать новые лиды (id группы с партнёрами, например -1001234567890). Пусто — только админам.
LEAD_CHAT_ID = int(os.getenv("LEAD_CHAT_ID")) if os.getenv("LEAD_CHAT_ID", "").lstrip("-").isdigit() else None

# ---------- Экономика ----------
FREE_LETTERS_ON_START = 3        # бесплатные разборы новому пользователю
REFERRAL_BONUS = 3               # сколько разборов получает пригласивший за друга
REFERRAL_WELCOME_BONUS = 1       # доп. бонус приглашённому (стимул перейти по ссылке)

# Пакеты за Telegram Stars: (код, название, кол-во разборов, цена в Stars)
PACKAGES = [
    ("p10", "10 писем", 10, 50),
    ("p30", "30 писем", 30, 120),
    ("p100", "100 писем", 100, 300),
]

MAX_IMAGE_MB = 10
MAX_PAGES = 5            # максимум страниц одного письма (альбом фото)
TIMEZONE = "Asia/Jerusalem"
