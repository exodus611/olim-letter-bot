"""SQLite-хранилище: пользователи, кредиты, рефералы, разборы, платежи, напоминания, лиды."""
import sqlite3
import time
from contextlib import contextmanager

from config import DB_PATH, FREE_LETTERS_ON_START, REFERRAL_BONUS, REFERRAL_WELCOME_BONUS


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                credits     INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER,
                created_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS letters (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                org_id       TEXT,
                letter_type  TEXT,
                urgency      TEXT,
                amount_ils   REAL,
                deadline     TEXT,
                scam_risk    TEXT,
                needs_spec   INTEGER DEFAULT 0,
                spec_type    TEXT,
                title        TEXT,
                created_at   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                letter_id   INTEGER NOT NULL,
                deadline    TEXT NOT NULL,      -- YYYY-MM-DD
                what        TEXT,
                title       TEXT,
                fire_at     INTEGER NOT NULL,   -- unix ts
                kind        TEXT NOT NULL,      -- d7 | d2 | d0
                sent        INTEGER NOT NULL DEFAULT 0,
                cancelled   INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS leads (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                letter_id   INTEGER,
                spec_type   TEXT,
                org_id      TEXT,
                letter_type TEXT,
                amount_ils  REAL,
                phone       TEXT,
                note        TEXT,
                status      TEXT NOT NULL DEFAULT 'new',   -- new | sent | closed
                created_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                package     TEXT NOT NULL,
                stars       INTEGER NOT NULL,
                credits     INTEGER NOT NULL,
                charge_id   TEXT,
                created_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                letter_id   INTEGER,
                useful      INTEGER NOT NULL,
                created_at  INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_fire ON reminders(sent, cancelled, fire_at);
            """
        )


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------- пользователи / кредиты
def get_or_create_user(user_id: int, username: str | None, first_name: str | None,
                       referrer_id: int | None = None) -> tuple[sqlite3.Row, bool]:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
                      (username, first_name, user_id))
            return row, False

        credits = FREE_LETTERS_ON_START
        valid_ref = None
        if referrer_id and referrer_id != user_id:
            ref = c.execute("SELECT user_id FROM users WHERE user_id=?", (referrer_id,)).fetchone()
            if ref:
                valid_ref = referrer_id
                credits += REFERRAL_WELCOME_BONUS
                c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?",
                          (REFERRAL_BONUS, referrer_id))

        c.execute(
            "INSERT INTO users(user_id, username, first_name, credits, referred_by, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, username, first_name, credits, valid_ref, int(time.time())),
        )
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone(), True


def get_credits(user_id: int) -> int:
    with _conn() as c:
        row = c.execute("SELECT credits FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["credits"] if row else 0


def spend_credit(user_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE users SET credits = credits - 1 WHERE user_id=? AND credits > 0", (user_id,))
        return cur.rowcount == 1


def refund_credit(user_id: int) -> None:
    add_credits(user_id, 1)


def add_credits(user_id: int, amount: int) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET credits = credits + ? WHERE user_id=?", (amount, user_id))


def referral_count(user_id: int) -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (user_id,)).fetchone()[0]


def all_user_ids() -> list[int]:
    with _conn() as c:
        return [r["user_id"] for r in c.execute("SELECT user_id FROM users")]


# ----------------------------------------------------------------- разборы
def log_letter(user_id: int, r: dict) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO letters(user_id, org_id, letter_type, urgency, amount_ils, deadline, scam_risk, "
            "needs_spec, spec_type, title, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, r.get("org_id"), r.get("letter_type"), r.get("urgency"),
             _num(r.get("amount_ils")), r.get("deadline"), r.get("scam_risk"),
             1 if r.get("needs_specialist") else 0, r.get("specialist_type"),
             (r.get("title") or "")[:120], int(time.time())),
        )
        return cur.lastrowid


def get_letter(letter_id: int) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM letters WHERE id=?", (letter_id,)).fetchone()


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------- напоминания
REMINDER_OFFSETS = {"d7": 7, "d2": 2, "d0": 0}   # за сколько дней до срока


def schedule_reminders(user_id: int, letter_id: int, deadline_iso: str, what: str | None,
                       title: str | None, now_ts: int | None = None, hour_local: int = 9,
                       tz_offset_hours: int = 3) -> int:
    """Ставит напоминания за 7 дней, за 2 дня и в день срока (в 09:00 по Израилю). Возвращает число созданных."""
    import datetime as dt
    now_ts = now_ts or int(time.time())
    try:
        d = dt.date.fromisoformat(deadline_iso)
    except ValueError:
        return 0
    created = 0
    with _conn() as c:
        for kind, days in REMINDER_OFFSETS.items():
            fire_day = d - dt.timedelta(days=days)
            fire_dt = dt.datetime(fire_day.year, fire_day.month, fire_day.day, hour_local,
                                  tzinfo=dt.timezone(dt.timedelta(hours=tz_offset_hours)))
            fire_at = int(fire_dt.timestamp())
            if fire_at <= now_ts + 60:   # уже прошло — не ставим
                continue
            c.execute(
                "INSERT INTO reminders(user_id, letter_id, deadline, what, title, fire_at, kind) VALUES(?,?,?,?,?,?,?)",
                (user_id, letter_id, deadline_iso, what, title, fire_at, kind),
            )
            created += 1
    return created


def due_reminders(now_ts: int | None = None) -> list[sqlite3.Row]:
    now_ts = now_ts or int(time.time())
    with _conn() as c:
        return c.execute(
            "SELECT * FROM reminders WHERE sent=0 AND cancelled=0 AND fire_at<=? ORDER BY fire_at LIMIT 100",
            (now_ts,),
        ).fetchall()


def mark_reminder_sent(reminder_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE reminders SET sent=1 WHERE id=?", (reminder_id,))


def cancel_reminders(user_id: int, letter_id: int) -> int:
    with _conn() as c:
        cur = c.execute("UPDATE reminders SET cancelled=1 WHERE user_id=? AND letter_id=? AND sent=0",
                        (user_id, letter_id))
        return cur.rowcount


def active_reminders(user_id: int) -> list[sqlite3.Row]:
    """Ближайшее активное напоминание по каждому письму."""
    with _conn() as c:
        return c.execute(
            "SELECT letter_id, deadline, what, title, MIN(fire_at) fire_at FROM reminders "
            "WHERE user_id=? AND sent=0 AND cancelled=0 GROUP BY letter_id ORDER BY deadline",
            (user_id,),
        ).fetchall()


# ----------------------------------------------------------------- лиды
def create_lead(user_id: int, letter_id: int | None, spec_type: str | None, phone: str, note: str | None) -> int:
    letter = get_letter(letter_id) if letter_id else None
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO leads(user_id, letter_id, spec_type, org_id, letter_type, amount_ils, phone, note, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (user_id, letter_id, spec_type or (letter["spec_type"] if letter else None),
             letter["org_id"] if letter else None, letter["letter_type"] if letter else None,
             letter["amount_ils"] if letter else None, phone, note, int(time.time())),
        )
        return cur.lastrowid


def leads(status: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    with _conn() as c:
        if status:
            return c.execute("SELECT * FROM leads WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        return c.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def set_lead_status(lead_id: int, status: str) -> None:
    with _conn() as c:
        c.execute("UPDATE leads SET status=? WHERE id=?", (status, lead_id))


# ----------------------------------------------------------------- платежи / фидбек
def log_payment(user_id: int, package: str, stars: int, credits: int, charge_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO payments(user_id, package, stars, credits, charge_id, created_at) VALUES(?,?,?,?,?,?)",
            (user_id, package, stars, credits, charge_id, int(time.time())),
        )


def log_feedback(user_id: int, letter_id: int | None, useful: bool) -> None:
    with _conn() as c:
        c.execute("INSERT INTO feedback(user_id, letter_id, useful, created_at) VALUES(?,?,?,?)",
                  (user_id, letter_id, int(useful), int(time.time())))


# ----------------------------------------------------------------- статистика
def stats() -> dict:
    now = int(time.time())
    day_ago, week_ago = now - 86400, now - 7 * 86400
    with _conn() as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731
        top_orgs = c.execute(
            "SELECT org_id, COUNT(*) n FROM letters WHERE org_id IS NOT NULL GROUP BY org_id ORDER BY n DESC LIMIT 6"
        ).fetchall()
        top_types = c.execute(
            "SELECT letter_type, COUNT(*) n FROM letters WHERE letter_type IS NOT NULL GROUP BY letter_type ORDER BY n DESC LIMIT 6"
        ).fetchall()
        return {
            "users_total": q("SELECT COUNT(*) FROM users"),
            "users_24h": q("SELECT COUNT(*) FROM users WHERE created_at>?", day_ago),
            "users_7d": q("SELECT COUNT(*) FROM users WHERE created_at>?", week_ago),
            "users_via_referral": q("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL"),
            "letters_total": q("SELECT COUNT(*) FROM letters"),
            "letters_24h": q("SELECT COUNT(*) FROM letters WHERE created_at>?", day_ago),
            "active_7d": q("SELECT COUNT(DISTINCT user_id) FROM letters WHERE created_at>?", week_ago),
            "scam_high": q("SELECT COUNT(*) FROM letters WHERE scam_risk='high'"),
            "needs_spec": q("SELECT COUNT(*) FROM letters WHERE needs_spec=1"),
            "reminders_active": q("SELECT COUNT(*) FROM reminders WHERE sent=0 AND cancelled=0"),
            "leads_total": q("SELECT COUNT(*) FROM leads"),
            "leads_new": q("SELECT COUNT(*) FROM leads WHERE status='new'"),
            "stars_total": q("SELECT COALESCE(SUM(stars),0) FROM payments"),
            "payments_total": q("SELECT COUNT(*) FROM payments"),
            "useful_yes": q("SELECT COUNT(*) FROM feedback WHERE useful=1"),
            "useful_no": q("SELECT COUNT(*) FROM feedback WHERE useful=0"),
            "top_orgs": [(r["org_id"], r["n"]) for r in top_orgs],
            "top_types": [(r["letter_type"], r["n"]) for r in top_types],
        }
