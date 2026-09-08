# Деплой бота: Railway / Oracle Free / Cloudflare

## Вариант 1 — Railway (проще всего, ~$5/мес)

Бот заезжает как есть. Нужен аккаунт GitHub.

1. **Залей проект на GitHub** (приватный репозиторий). Файл `.env` НЕ заливай — он в `.dockerignore`,
   но проверь, что и в git он не попал (`.gitignore` ниже).
2. Railway → **New Project → Deploy from GitHub repo** → выбери репозиторий.
   Railway сам увидит `Dockerfile` и `railway.json`.
3. **Variables** (вкладка сервиса) — добавь:
   ```
   BOT_TOKEN=...
   GEMINI_API_KEY=...
   ADMIN_IDS=твой_user_id
   DB_PATH=/data/bot.db
   ```
4. **Volume** — обязательно, иначе база (пользователи, кредиты, напоминания) сотрётся при
   каждом передеплое: правой кнопкой по сервису → *Add Volume* → mount path `/data`.
5. **Settings → Networking**: публичный домен НЕ нужен (бот сам ходит к Telegram). Ничего не включай.
6. **Лимит расходов** (чтобы точно «не хавал»): Workspace → Usage → *Set usage limit* → например $6.
   При достижении лимита сервис остановится, а не начнёт списывать больше.
7. Deploy. В логах должно появиться `Bot @имя started`. Напиши боту `/start`.

Сколько ест: ~120 МБ RAM, CPU почти ноль → около $1–2/мес потребления, что укладывается
в $5 включённых в Hobby. Итого ровно $5/мес.

Обновление: `git push` → Railway передеплоит сам за минуту.

## Вариант 2 — Oracle Cloud Always Free (бесплатно навсегда)

1. Зарегистрируйся на cloud.oracle.com (карта нужна для проверки, не списывают).
2. Создай инстанс: shape **VM.Standard.A1.Flex** (ARM, Always Free), образ Ubuntu 22.04/24.04,
   1 OCPU / 6 GB — хватит с запасом. Если пишет «Out of capacity» — попробуй другой
   availability domain или повтори через день-два (частая история).
3. Зайди по SSH и:
   ```bash
   sudo apt update && sudo apt install -y python3-pip python3-venv git
   git clone <твой_репозиторий> olim-letter-bot && cd olim-letter-bot
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env && nano .env      # заполни токены
   python test_letter.py --demo           # проверка
   ```
4. Автозапуск через systemd:
   ```bash
   sudo tee /etc/systemd/system/letterbot.service >/dev/null <<EOF
   [Unit]
   Description=Olim letter bot
   After=network.target
   [Service]
   User=ubuntu
   WorkingDirectory=/home/ubuntu/olim-letter-bot
   ExecStart=/home/ubuntu/olim-letter-bot/.venv/bin/python bot.py
   Restart=always
   RestartSec=5
   [Install]
   WantedBy=multi-user.target
   EOF
   sudo systemctl enable --now letterbot
   sudo journalctl -u letterbot -f        # логи
   ```
5. Бэкап базы раз в сутки (cron): `0 3 * * * cp /home/ubuntu/olim-letter-bot/bot.db /home/ubuntu/backup-$(date +\%F).db`

## Вариант 3 — Cloudflare Workers (бесплатно, но нужна переделка)

Текущий код на Python с long polling, SQLite и фоновым циклом — на Workers **не запускается**.
Что нужно для переноса:

| Сейчас | На Cloudflare |
|---|---|
| Python + aiogram, long polling | JS/TS, webhook (`setWebhook` на URL воркера) |
| SQLite-файл | Cloudflare D1 (бесплатно: 5 ГБ, 5 млн чтений/день) |
| Фоновый цикл напоминаний раз в минуту | Cron Trigger (до 5 на аккаунт на Free) |
| Скачивание фото → Gemini | fetch к api.telegram.org → fetch к Gemini (по 10 мс CPU на запрос на Free — сеть не считается, для фото хватает) |
| Оплата Stars, FSM для лидов | то же через Bot API, состояние — в D1 |

Это переписывание на другой язык, ~1 день работы. Логика (`knowledge.py`, промпт, сроки) переносится
один в один. Имеет смысл, если принципиально хочется $0 без карты и ты уже привык к Cloudflare.

## Что нельзя делать ни на одном хостинге

- Не заливай `.env` и `bot.db` в GitHub. Добавь `.gitignore`:
  ```
  .env
  *.db
  __pycache__/
  ```
- Не запускай бота **в двух местах одновременно** (например, дома и на Railway) — Telegram
  отдаёт обновления только одному, второй будет получать ошибки Conflict.
- Не держи базу вне volume/диска — потеряешь пользователей при передеплое.
