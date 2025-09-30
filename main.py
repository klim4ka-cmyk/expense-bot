import os
import logging
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # Railway Postgres plugin предоставит эту переменную

if not BOT_TOKEN:
    raise RuntimeError("Не указан TELEGRAM_BOT_TOKEN")
if not DATABASE_URL:
    raise RuntimeError("Не указан DATABASE_URL (подключи PostgreSQL на Railway)")

# Подключение к БД (один коннект на процесс)
conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
conn.autocommit = True

# Инициализация схемы
def init_db():
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            amount NUMERIC(12,2) NOT NULL,
            category TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
    logger.info("Схема БД готова")

def parse_expense(text: str):
    # Ожидаем формат: "<сумма> <категория>"
    parts = text.strip().split(maxsplit=2)
    if not parts:
        return None
    try:
        amount = float(parts[0].replace(",", "."))
    except ValueError:
        return None
    category = parts[1] if len(parts) > 1 else "прочее"
    # Если есть третий кусок — добавим в категорию (например: "еда кафе")
    if len(parts) == 3:
        category = f"{category} {parts[2]}"
    return amount, category.lower()

def get_period_bounds(period: str):
    now = datetime.now(timezone.utc)
    period = (period or "").lower()
    if period in ("day", "сегодня", "день"):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif period in ("week", "неделя"):
        # ISO неделя: понедельник — начало
        start_of_week = now - timedelta(days=(now.weekday()))
        start = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    else:
        # По умолчанию — текущий месяц
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
    return start, end

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я трекер расходов 💸\n\n"
        "Добавляй расходы сообщениями:\n"
        "Например: 200 продукты\n"
        "или: 15 кофе\n\n"
        "Команды:\n"
        "/stats — статистика за месяц\n"
        "/stats день — за сегодня\n"
        "/stats неделя — за неделю\n"
        "/help — справка"
    )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Формат добавления: '<сумма> <категория>'\n"
        "Примеры:\n"
        "— 200 продукты\n"
        "— 50 транспорт\n\n"
        "Команды:\n"
        "/stats [день|неделя] — показать статистику\n"
        "/start — начать"
    )

async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    parsed = parse_expense(msg)
    if not parsed:
        await update.message.reply_text("Формат: '<сумма> <категория>'. Например: 150 кофе")
        return
    amount, category = parsed
    user_id = update.effective_user.id

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO expenses (user_id, amount, category) VALUES (%s, %s, %s)",
            (user_id, amount, category)
        )

    await update.message.reply_text(f"Добавлено: {amount:.2f} — {category}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Параметр периода из команды
    period_arg = " ".join(context.args) if context.args else None
    start, end = get_period_bounds(period_arg)
    user_id = update.effective_user.id

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT category, SUM(amount)::numeric(12,2) AS total
            FROM expenses
            WHERE user_id = %s AND created_at >= %s AND created_at <= %s
            GROUP BY category
            ORDER BY total DESC;
            """,
            (user_id, start, end)
        )
        rows = cur.fetchall()

        cur.execute(
            """
            SELECT COALESCE(SUM(amount), 0)::numeric(12,2) AS grand_total
            FROM expenses
            WHERE user_id = %s AND created_at >= %s AND created_at <= %s;
            """,
            (user_id, start, end)
        )
        grand_total = cur.fetchone()["grand_total"]

    period_name = "месяц"
    if period_arg and period_arg.lower() in ("day", "сегодня", "день"):
        period_name = "сегодня"
    elif period_arg and period_arg.lower() in ("week", "неделя"):
        period_name = "неделя"

    if not rows:
        await update.message.reply_text(f"За период «{period_name}» расходов нет.")
        return

    lines = [f"📊 Статистика за {period_name}:"]
    for r in rows:
        lines.append(f"- {r['category']}: {r['total']} руб.")
    lines.append(f"\nИтого: {grand_total} руб.")
    await update.message.reply_text("\n".join(lines))

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_expense))

    logger.info("Бот запущен (polling)")
    app.run_polling()

if __name__ == "__main__":
    main()
