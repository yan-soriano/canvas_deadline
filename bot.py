import os, sqlite3, logging, asyncio
from datetime import datetime, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
import aiohttp

# Загружаем переменные из .env файла
load_dotenv()

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан! Создай .env файл с TELEGRAM_TOKEN=твой_токен")

CANVAS_URL_DEFAULT = os.getenv("CANVAS_URL", "https://canvas.instructure.com")
# Проверка каждые 30 минут — чтобы попадать в окна напоминалок (1ч, 2ч и т.д.)
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "30"))
DB_PATH = os.getenv("DB_PATH", "deadlines.db")

# Пороги напоминаний о дедлайнах (в часах) с допустимым окном
# threshold_key: (часы_до_дедлайна, мин_окно, макс_окно, иконка, текст)
DEADLINE_THRESHOLDS = [
    ("24h", 24, 23.0, 25.0,  "📅", "24 часа"),
    ("12h", 12, 11.0, 13.0,  "⚠️", "12 часов"),
    ("8h",   8,  7.0,  9.0,  "⚠️", "8 часов"),
    ("6h",   6,  5.5,  6.5,  "🔔", "6 часов"),
    ("5h",   5,  4.5,  5.5,  "🔔", "5 часов"),
    ("4h",   4,  3.5,  4.5,  "🔔", "4 часа"),
    ("2h",   2,  1.5,  2.5,  "🚨", "2 часа"),
    ("1h",   1,  0.5,  1.5,  "🚨", "1 час"),
]

# ────────────────────────────────────────────────
# DATABASE
# ────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        canvas_token TEXT NOT NULL,
        canvas_url TEXT NOT NULL,
        notify_deadlines INTEGER DEFAULT 1,
        notify_grades INTEGER DEFAULT 1,
        notify_new_assignments INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS sent_notifications (
        chat_id INTEGER,
        item_id TEXT,
        notify_type TEXT,
        PRIMARY KEY (chat_id, item_id, notify_type)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS seen_grades (
        chat_id INTEGER,
        assignment_id TEXT,
        score TEXT,
        PRIMARY KEY (chat_id, assignment_id)
    )""")
    # Таблица для отслеживания известных заданий (для уведомлений о новых)
    c.execute("""CREATE TABLE IF NOT EXISTS known_assignments (
        chat_id INTEGER,
        assignment_id TEXT,
        PRIMARY KEY (chat_id, assignment_id)
    )""")
    conn.commit()
    conn.close()

def _migrate_db():
    """Мигрирует старую схему БД к новой, если нужно."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Проверяем, есть ли новый столбец notify_new_assignments
    c.execute("PRAGMA table_info(users)")
    cols = [row[1] for row in c.fetchall()]
    if "notify_new_assignments" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN notify_new_assignments INTEGER DEFAULT 1")
    # Удаляем старые столбцы, которые больше не нужны (SQLite не поддерживает DROP COLUMN до 3.35)
    # Просто игнорируем их при чтении
    # Создаём таблицу known_assignments если нет
    c.execute("""CREATE TABLE IF NOT EXISTS known_assignments (
        chat_id INTEGER,
        assignment_id TEXT,
        PRIMARY KEY (chat_id, assignment_id)
    )""")
    conn.commit()
    conn.close()

def get_user(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, canvas_token, canvas_url, notify_deadlines, notify_grades, notify_new_assignments FROM users WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, canvas_token, canvas_url, notify_deadlines, notify_grades, notify_new_assignments FROM users")
    rows = c.fetchall()
    conn.close()
    return rows

def save_user(chat_id, token, url):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO users (chat_id, canvas_token, canvas_url)
        VALUES (?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
        canvas_token=excluded.canvas_token, canvas_url=excluded.canvas_url""",
        (chat_id, token, url))
    conn.commit()
    conn.close()

def update_settings(chat_id, col, val):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"UPDATE users SET {col}=? WHERE chat_id=?", (val, chat_id))
    conn.commit()
    conn.close()

def already_sent(chat_id, item_id, ntype):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM sent_notifications WHERE chat_id=? AND item_id=? AND notify_type=?",
              (chat_id, str(item_id), ntype))
    r = c.fetchone() is not None
    conn.close()
    return r

def mark_sent(chat_id, item_id, ntype):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO sent_notifications VALUES(?,?,?)", (chat_id, str(item_id), ntype))
    conn.commit()
    conn.close()

def get_seen_grade(chat_id, assignment_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT score FROM seen_grades WHERE chat_id=? AND assignment_id=?", (chat_id, str(assignment_id)))
    r = c.fetchone()
    conn.close()
    return r[0] if r else None

def save_grade(chat_id, assignment_id, score):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO seen_grades VALUES(?,?,?)", (chat_id, str(assignment_id), str(score)))
    conn.commit()
    conn.close()

def is_assignment_known(chat_id, assignment_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM known_assignments WHERE chat_id=? AND assignment_id=?",
              (chat_id, str(assignment_id)))
    r = c.fetchone() is not None
    conn.close()
    return r

def mark_assignment_known(chat_id, assignment_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO known_assignments VALUES(?,?)", (chat_id, str(assignment_id)))
    conn.commit()
    conn.close()

# ────────────────────────────────────────────────
# CANVAS API HELPERS
# ────────────────────────────────────────────────
async def canvas_get(session, url, token, params=None):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with session.get(url, headers=headers, params=params or {}, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 200:
                return await r.json()
            logger.warning(f"Canvas API {r.status}: {url}")
            return None
    except Exception as e:
        logger.error(f"Canvas API error: {e}")
        return None

async def canvas_get_all_pages(session, url, token, params=None):
    """Получает все страницы результатов из Canvas API."""
    headers = {"Authorization": f"Bearer {token}"}
    all_results = []
    params = dict(params or {})
    params.setdefault("per_page", "100")
    current_url = url

    while current_url:
        try:
            async with session.get(current_url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    break
                data = await r.json()
                if isinstance(data, list):
                    all_results.extend(data)
                else:
                    break

                # Парсим Link header для пагинации
                link_header = r.headers.get("Link", "")
                next_url = None
                for part in link_header.split(","):
                    if 'rel="next"' in part:
                        next_url = part.split(";")[0].strip().strip("<>")
                        break
                current_url = next_url
                params = {}  # params уже в URL для следующих страниц
        except Exception as e:
            logger.error(f"Canvas API pagination error: {e}")
            break

    return all_results

async def get_courses(session, token, canvas_url):
    return await canvas_get(session, f"{canvas_url}/api/v1/courses",
        token, {"enrollment_state": "active", "per_page": 100}) or []

# ────────────────────────────────────────────────
# FETCH: Параллельные запросы к курсам
# ────────────────────────────────────────────────
async def _fetch_course_assignments(session, token, canvas_url, course, include_submission=False):
    """Получает задания одного курса (вызывается параллельно для всех курсов)."""
    cid = course['id']
    params = {"per_page": 100}
    if include_submission:
        params["include[]"] = "submission"
    items = await canvas_get(session,
        f"{canvas_url}/api/v1/courses/{cid}/assignments",
        token, params) or []
    return course, items

async def fetch_all_data(token, canvas_url, chat_id=None):
    """
    Единый fetch: получает курсы, затем ПАРАЛЛЕЛЬНО запрашивает
    задания всех курсов. Возвращает (upcoming, all_assignments, new_grades, courses).
    
    Если chat_id задан — также обрабатывает оценки.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=30)  # только дедлайны на ближайшие 30 дней
    upcoming = []
    all_assignments = []
    new_grades = []

    connector = aiohttp.TCPConnector(limit=10)  # до 10 параллельных соединений
    async with aiohttp.ClientSession(connector=connector) as s:
        courses = await get_courses(s, token, canvas_url)
        active_courses = [c for c in courses if not c.get("access_restricted_by_date")]

        if not active_courses:
            return upcoming, all_assignments, new_grades, courses

        # Параллельно запрашиваем задания ВСЕХ курсов (с submission для полных данных)
        tasks = [
            _fetch_course_assignments(s, token, canvas_url, course, include_submission=True)
            for course in active_courses
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Error fetching course: {result}")
                continue
            course, items = result
            cid = course['id']
            cname = course.get('name', 'Курс')

            for a in items:
                aid = str(a["id"])

                # ── Собираем ВСЕ задания (для отслеживания новых) ──
                all_assignments.append({
                    "id": a["id"],
                    "name": a.get("name", "Задание"),
                    "course": cname,
                    "due_at_str": a.get("due_at"),
                    "url": a.get("html_url", ""),
                    "points": a.get("points_possible"),
                })

                # ── Предстоящие дедлайны (только ближайшие 30 дней) ──
                due = a.get("due_at")
                if due:
                    try:
                        dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                    except Exception:
                        dt = None
                    if dt and now < dt <= cutoff:
                        submission = a.get("submission", {}) or {}
                        submitted = submission.get("workflow_state") in (
                            "submitted", "graded", "pending_review")
                        if not submitted and submission.get("submitted_at"):
                            submitted = True

                        upcoming.append({
                            "id": a["id"],
                            "name": a.get("name", "Задание"),
                            "course": cname,
                            "course_id": cid,
                            "due_at": dt,
                            "url": a.get("html_url", ""),
                            "points": a.get("points_possible"),
                            "submitted": submitted,
                        })

                # ── Оценки ──
                if chat_id is not None:
                    sub = a.get("submission", {}) or {}
                    score = sub.get("score")
                    grade = sub.get("grade")
                    if score is not None or grade is not None:
                        seen = get_seen_grade(chat_id, aid)
                        score_str = str(score if score is not None else grade)
                        if seen != score_str:
                            save_grade(chat_id, aid, score_str)
                            if seen is not None:
                                pts = a.get("points_possible")
                                new_grades.append({
                                    "name": a.get("name", "Задание"),
                                    "course": cname,
                                    "score": score, "grade": grade, "points": pts,
                                    "url": a.get("html_url", ""),
                                })

    upcoming.sort(key=lambda x: x["due_at"])
    return upcoming, all_assignments, new_grades, courses

# ────────────────────────────────────────────────
# SCHEDULER: check all & notify
# ────────────────────────────────────────────────
async def check_and_notify(app):
    users = get_all_users()
    now = datetime.now(timezone.utc)
    logger.info(f"Running check for {len(users)} users...")

    for u in users:
        chat_id, token, canvas_url, n_dead, n_grades, n_new_assign = u

        try:
            # Один fetch — все данные параллельно
            upcoming, all_assignments, new_grades, _ = await fetch_all_data(
                token, canvas_url, chat_id=chat_id if n_grades else None)

            # ═══ НОВЫЕ ЗАДАНИЯ ═══
            if n_new_assign:
                for a in all_assignments:
                    aid = str(a["id"])
                    if not is_assignment_known(chat_id, aid):
                        mark_assignment_known(chat_id, aid)
                        # Проверяем, не первая ли это синхронизация
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute("SELECT COUNT(*) FROM known_assignments WHERE chat_id=?", (chat_id,))
                        count = c.fetchone()[0]
                        conn.close()

                        if count > 1:
                            due_str = ""
                            if a.get("due_at_str"):
                                try:
                                    dt = datetime.fromisoformat(a["due_at_str"].replace("Z", "+00:00"))
                                    due_str = f"\n🕐 Дедлайн: {dt.strftime('%d.%m.%Y %H:%M')} UTC"
                                except:
                                    pass
                            pts = f"\n💯 Баллов: {int(a['points'])}" if a.get("points") else ""
                            url = f"\n🔗 {a['url']}" if a.get("url") else ""
                            try:
                                await app.bot.send_message(chat_id,
                                    f"🆕 *Новое задание!*\n"
                                    f"📚 {a['course']}\n"
                                    f"📝 {a['name']}{pts}{due_str}{url}",
                                    parse_mode="Markdown")
                            except Exception as e:
                                logger.error(f"Error sending new assignment notification to {chat_id}: {e}")

            # ═══ ДЕДЛАЙНЫ (с проверкой сдачи) ═══
            if n_dead:
                for a in upcoming:
                    if a.get("submitted"):
                        continue

                    hours_left = (a["due_at"] - now).total_seconds() / 3600

                    for (key, _nominal, lo, hi, icon, label) in DEADLINE_THRESHOLDS:
                        if lo <= hours_left <= hi:
                            if not already_sent(chat_id, a["id"], key):
                                pts = f" · {int(a['points'])} б." if a.get("points") else ""
                                due_str = a["due_at"].strftime("%d.%m %H:%M UTC")
                                url = f"\n🔗 {a['url']}" if a.get("url") else ""
                                try:
                                    await app.bot.send_message(chat_id,
                                        f"{icon} *Дедлайн через {label}!*\n"
                                        f"📚 {a['course']}\n"
                                        f"📝 {a['name']}{pts}\n"
                                        f"🕐 {due_str}{url}",
                                        parse_mode="Markdown")
                                    mark_sent(chat_id, a["id"], key)
                                except Exception as e:
                                    logger.error(f"Error sending deadline notification to {chat_id}: {e}")

            # ═══ ОЦЕНКИ ═══
            if n_grades:
                for g in new_grades:
                    pts = f"/{int(g['points'])}" if g.get("points") else ""
                    score_str = str(g["score"]) if g["score"] is not None else g.get("grade", "?")
                    url = f"\n🔗 {g['url']}" if g.get("url") else ""
                    try:
                        await app.bot.send_message(chat_id,
                            f"📊 *Новая оценка!*\n"
                            f"📚 {g['course']}\n"
                            f"📝 {g['name']}\n"
                            f"✏️ {score_str}{pts}{url}",
                            parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Error sending grade notification to {chat_id}: {e}")

        except Exception as e:
            logger.error(f"Error processing user {chat_id}: {e}")

    logger.info(f"Check done for {len(users)} users.")

# ────────────────────────────────────────────────
# TELEGRAM: клавиатура и команды
# ────────────────────────────────────────────────
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📅 Дедлайны"), KeyboardButton("📊 Оценки")],
        [KeyboardButton("🔄 Проверить"), KeyboardButton("⚙️ Настройки")],
        [KeyboardButton("🧪 Тест"), KeyboardButton("🔑 Сменить токен")],
    ],
    resize_keyboard=True
)

# Соответствие текста кнопок → функции команд
BUTTON_MAP = {
    "📅 Дедлайны": "deadlines",
    "📊 Оценки": "grades",
    "🔄 Проверить": "check",
    "⚙️ Настройки": "settings",
    "🧪 Тест": "test",
    "🔑 Сменить токен": "reconnect",
}
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_chat.id)
    if user:
        await update.message.reply_text(
            "Ты уже подключён! ✅\n"
            "Используй кнопки ниже 👇",
            reply_markup=MAIN_KEYBOARD
        )
    else:
        await update.message.reply_text(
            "👋 *Canvas Deadline Bot*\n\n"
            "Слежу за дедлайнами, оценками и новыми заданиями в Canvas LMS.\n\n"
            "📌 *Как получить токен:*\n"
            "1. Canvas → Account → Settings\n"
            "2. Scroll вниз → *Approved Integrations*\n"
            "3. *New Access Token* → скопируй\n\n"
            "Если Canvas на другом домене — сначала:\n"
            "`/seturl https://myuniversity.instructure.com`\n\n"
            "Затем отправь мне токен 👇",
            parse_mode="Markdown"
        )
        ctx.user_data["waiting_for"] = "token"

async def cmd_seturl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Использование:\n`/seturl https://uni.instructure.com`", parse_mode="Markdown")
        return
    ctx.user_data["canvas_url"] = parts[1].rstrip("/")
    await update.message.reply_text(f"✅ URL сохранён. Теперь отправь токен Canvas.")
    ctx.user_data["waiting_for"] = "token"

async def cmd_reconnect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отправь новый API-токен Canvas:")
    ctx.user_data["waiting_for"] = "token"

async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Принудительная проверка — запускает цикл check_and_notify прямо сейчас."""
    u = get_user(update.effective_chat.id)
    if not u:
        return await update.message.reply_text("Сначала /start")
    msg = await update.message.reply_text("🔄 Запускаю проверку...")

    chat_id = update.effective_chat.id
    token, canvas_url = u[1], u[2]
    now = datetime.now(timezone.utc)

    upcoming, all_assignments, new_grades, courses = await fetch_all_data(
        token, canvas_url, chat_id=chat_id)

    # Считаем статистику
    not_submitted = [a for a in upcoming if not a.get("submitted")]
    submitted = [a for a in upcoming if a.get("submitted")]

    # Проверяем, какие дедлайны попадают в окна напоминалок
    deadline_alerts = []
    for a in not_submitted:
        hours_left = (a["due_at"] - now).total_seconds() / 3600
        for (key, _nominal, lo, hi, icon, label) in DEADLINE_THRESHOLDS:
            if lo <= hours_left <= hi:
                sent = already_sent(chat_id, a["id"], key)
                status = "✅ уже отправлено" if sent else "📤 будет отправлено"
                deadline_alerts.append(f"  {icon} {a['name']} — через {label} ({status})")

    text = (
        f"📊 *Результаты проверки:*\n\n"
        f"📚 Активных курсов: {len(courses)}\n"
        f"📝 Всего заданий: {len(all_assignments)}\n"
        f"📅 Предстоящих дедлайнов: {len(upcoming)}\n"
        f"  ✅ Сдано: {len(submitted)}\n"
        f"  ⏳ Не сдано: {len(not_submitted)}\n"
        f"📊 Новых оценок: {len(new_grades)}\n"
    )

    if deadline_alerts:
        text += f"\n⏰ *Дедлайн-напоминалки в окне:*\n"
        text += "\n".join(deadline_alerts[:10]) + "\n"

    if new_grades:
        text += f"\n📊 *Новые оценки:*\n"
        for g in new_grades[:5]:
            score_str = str(g["score"]) if g["score"] is not None else g.get("grade", "?")
            text += f"  ✏️ {g['name']} — {score_str}\n"

    await msg.edit_text(text, parse_mode="Markdown")

async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Отправляет тестовые уведомления — чтобы увидеть как они выглядят."""
    u = get_user(update.effective_chat.id)
    if not u:
        return await update.message.reply_text("Сначала /start")
    chat_id = update.effective_chat.id

    # Тестовое уведомление: новое задание
    await update.message.reply_text(
        "🆕 *Новое задание!*\n"
        "📚 Математический анализ\n"
        "📝 Домашнее задание №5\n"
        "💯 Баллов: 100\n"
        "🕐 Дедлайн: 20.03.2026 23:59 UTC\n"
        "🔗 https://canvas.example.com/assignment/123",
        parse_mode="Markdown")

    await asyncio.sleep(0.5)

    # Тестовое уведомление: дедлайн
    await update.message.reply_text(
        "🚨 *Дедлайн через 2 часа!*\n"
        "📚 Программирование на Python\n"
        "📝 Лабораторная работа №3 · 50 б.\n"
        "🕐 17.03 23:59 UTC\n"
        "🔗 https://canvas.example.com/assignment/456",
        parse_mode="Markdown")

    await asyncio.sleep(0.5)

    # Тестовое уведомление: оценка
    await update.message.reply_text(
        "📊 *Новая оценка!*\n"
        "📚 История Казахстана\n"
        "📝 Эссе: Великий Шёлковый путь\n"
        "✏️ 85/100\n"
        "🔗 https://canvas.example.com/assignment/789",
        parse_mode="Markdown")

    await asyncio.sleep(0.5)

    await update.message.reply_text(
        "☝️ Это были *тестовые* уведомления.\n\n"
        "Для реальной проверки используй /check — \n"
        "он покажет что бот видит в Canvas прямо сейчас.",
        parse_mode="Markdown")

async def cmd_deadlines(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_chat.id)
    if not u:
        return await update.message.reply_text("Сначала /start")
    msg = await update.message.reply_text("⏳ Загружаю дедлайны...")
    upcoming, _, _, _ = await fetch_all_data(u[1], u[2])
    if not upcoming:
        return await msg.edit_text("🎉 Нет предстоящих дедлайнов!")

    now = datetime.now(timezone.utc)
    text = "📋 *Предстоящие дедлайны:*\n\n"
    for a in upcoming[:20]:
        h = (a["due_at"] - now).total_seconds() / 3600
        icon = "🚨" if h < 8 else ("⚠️" if h < 24 else "📅")
        if h < 1:
            left = f"{int(h * 60)}мин"
        elif h < 48:
            left = f"{int(h)}ч"
        else:
            left = f"{int(h // 24)}д"

        submitted_mark = " ✅" if a.get("submitted") else ""
        text += f"{icon} *{a['name']}*{submitted_mark}\n   {a['course']} · через {left}\n\n"

    if len(upcoming) > 20:
        text += f"_...и ещё {len(upcoming) - 20} заданий_\n"

    await msg.edit_text(text, parse_mode="Markdown")

async def cmd_grades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_chat.id)
    if not u:
        return await update.message.reply_text("Сначала /start")
    msg = await update.message.reply_text("⏳ Загружаю оценки...")
    async with aiohttp.ClientSession() as s:
        courses = await get_courses(s, u[1], u[2])
    if not courses:
        return await msg.edit_text("❌ Не удалось загрузить курсы.")
    text = "📊 *Текущие оценки:*\n\n"
    has_grades = False
    async with aiohttp.ClientSession() as s:
        for course in courses[:15]:
            if course.get("access_restricted_by_date"):
                continue
            enrollments = await canvas_get(s, f"{u[2]}/api/v1/courses/{course['id']}/enrollments",
                u[1], {"user_id": "self", "per_page": 5}) or []
            for en in enrollments:
                gr = en.get("grades", {})
                score = gr.get("current_score")
                grade = gr.get("current_grade")
                if score is not None or grade is not None:
                    display = f"{score}%" if score is not None else grade
                    text += f"📚 {course.get('name', '')}\n   ✏️ {display}\n\n"
                    has_grades = True
    if not has_grades:
        text = "Оценок пока нет."
    await msg.edit_text(text, parse_mode="Markdown")

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_chat.id)
    if not u:
        return await update.message.reply_text("Сначала /start")
    # u = (chat_id, token, url, n_dead, n_grades, n_new_assign)
    nd = u[3] if len(u) > 3 else 1
    ng = u[4] if len(u) > 4 else 1
    nna = u[5] if len(u) > 5 else 1

    kb = [
        [InlineKeyboardButton(f"{'✅' if nd else '❌'} Дедлайны", callback_data="tog_nd"),
         InlineKeyboardButton(f"{'✅' if ng else '❌'} Оценки", callback_data="tog_ng")],
        [InlineKeyboardButton(f"{'✅' if nna else '❌'} Новые задания", callback_data="tog_nna")],
        [InlineKeyboardButton("── Дедлайны: когда напоминать ──", callback_data="noop")],
        [InlineKeyboardButton("24ч · 12ч · 8ч · 6ч · 5ч · 4ч · 2ч · 1ч", callback_data="noop")],
    ]
    await update.message.reply_text(
        "⚙️ *Настройки уведомлений*\n\n"
        "Напоминания о дедлайне приходят за:\n"
        "24ч → 12ч → 8ч → 6ч → 5ч → 4ч → 2ч → 1ч\n\n"
        "_Если работа уже сдана — напоминания не приходят_ ✅",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

TOGGLE_MAP = {
    "tog_nd": ("notify_deadlines", 3),
    "tog_ng": ("notify_grades", 4),
    "tog_nna": ("notify_new_assignments", 5),
}

async def handle_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "noop":
        return
    chat_id = q.message.chat_id
    u = get_user(chat_id)
    if not u:
        return
    toggle_info = TOGGLE_MAP.get(q.data)
    if not toggle_info:
        return
    col, idx = toggle_info
    old_val = u[idx] if idx < len(u) else 1
    new_val = 1 - old_val
    update_settings(chat_id, col, new_val)

    # Перечитываем пользователя
    u = get_user(chat_id)
    nd = u[3] if len(u) > 3 else 1
    ng = u[4] if len(u) > 4 else 1
    nna = u[5] if len(u) > 5 else 1

    kb = [
        [InlineKeyboardButton(f"{'✅' if nd else '❌'} Дедлайны", callback_data="tog_nd"),
         InlineKeyboardButton(f"{'✅' if ng else '❌'} Оценки", callback_data="tog_ng")],
        [InlineKeyboardButton(f"{'✅' if nna else '❌'} Новые задания", callback_data="tog_nna")],
        [InlineKeyboardButton("── Дедлайны: когда напоминать ──", callback_data="noop")],
        [InlineKeyboardButton("24ч · 12ч · 8ч · 6ч · 5ч · 4ч · 2ч · 1ч", callback_data="noop")],
    ]
    await q.edit_message_reply_markup(InlineKeyboardMarkup(kb))

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    if ctx.user_data.get("waiting_for") == "token":
        canvas_url = ctx.user_data.get("canvas_url", CANVAS_URL_DEFAULT)
        msg = await update.message.reply_text("🔄 Проверяю токен Canvas...")
        try:
            # Проверяем токен — пробуем получить курсы
            async with aiohttp.ClientSession() as s:
                courses = await get_courses(s, text, canvas_url)
            if not courses:
                await msg.edit_text("❌ Не удалось получить курсы. Проверь токен или URL.\n"
                                   "Укажи URL через /seturl если нужно.")
                return

            save_user(chat_id, text, canvas_url)
            ctx.user_data.pop("waiting_for", None)

            # Первичная синхронизация — один параллельный запрос
            sync_msg = await msg.edit_text("✅ Токен принят! Синхронизирую данные...")

            _, all_assign, _, _ = await fetch_all_data(text, canvas_url, chat_id=chat_id)
            for a in all_assign:
                mark_assignment_known(chat_id, str(a["id"]))

            await sync_msg.edit_text(
                f"✅ *Подключено!*\n\n"
                f"📚 Курсов: {len(courses)}\n"
                f"📝 Заданий: {len(all_assign)}\n\n"
                f"*Уведомления:*\n"
                f"🆕 Новые задания\n"
                f"📊 Новые оценки\n"
                f"⏰ Дедлайны за 24ч/12ч/8ч/6ч/5ч/4ч/2ч/1ч\n"
                f"✅ Сданные работы — без напоминаний",
                parse_mode="Markdown")
            # Отправляем отдельное сообщение с клавиатурой
            await update.message.reply_text(
                "Используй кнопки ниже 👇",
                reply_markup=MAIN_KEYBOARD)
        except Exception as e:
            logger.error(f"Token verification error: {e}")
            await msg.edit_text(f"❌ Ошибка: {e}\nПроверь токен или укажи URL через /seturl")
    else:
        # Проверяем, не нажал ли пользователь кнопку меню
        cmd_name = BUTTON_MAP.get(text)
        if cmd_name:
            cmd_funcs = {
                "deadlines": cmd_deadlines,
                "grades": cmd_grades,
                "check": cmd_check,
                "settings": cmd_settings,
                "test": cmd_test,
                "reconnect": cmd_reconnect,
            }
            fn = cmd_funcs.get(cmd_name)
            if fn:
                await fn(update, ctx)
                return

        await update.message.reply_text(
            "Используй кнопки ниже 👇",
            reply_markup=MAIN_KEYBOARD)

# ────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────
async def post_init(app: Application):
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_and_notify,
        "interval", minutes=CHECK_INTERVAL_MINUTES,
        args=[app],
        next_run_time=datetime.now() + timedelta(seconds=30)
    )
    scheduler.start()
    logger.info(f"Scheduler started with interval: {CHECK_INTERVAL_MINUTES} min")

def main():
    init_db()
    _migrate_db()

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    for cmd, fn in [("start", cmd_start), ("deadlines", cmd_deadlines),
                    ("grades", cmd_grades), ("settings", cmd_settings),
                    ("seturl", cmd_seturl), ("reconnect", cmd_reconnect),
                    ("check", cmd_check), ("test", cmd_test)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(handle_toggle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info(f"Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
