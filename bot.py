# bot.py
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler
)
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# === 🔍 ОТЛАДКА: Проверка окружения ===
print("📁 Текущая папка:", os.getcwd())
print("📄 Файлы в папке:", os.listdir())

dotenv_path = os.path.join(os.getcwd(), ".env")
if os.path.exists(dotenv_path):
    print(f"✅ Файл .env найден по пути: {dotenv_path}")
    load_dotenv(dotenv_path)
else:
    print(f"❌ Файл .env НЕ НАЙДЕН по пути: {dotenv_path}")
    exit()

TOKEN = os.getenv("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

if not TOKEN or not SPREADSHEET_ID:
    print("❗ ОШИБКА: Не задан TELEGRAM_TOKEN или SPREADSHEET_ID в .env")
    exit()
else:
    print("✅ Переменные окружения загружены успешно!")

# === НАСТРОЙКИ ===
SHEET_NAME = "Лист прогнозов ЧМ"
RANGE = f"{SHEET_NAME}!A:ZZ"

AUTHORIZED_USERS = [
    283970723, 183590516, 146770254, 1129899475, 146921711,
    333946991, 402914102, 816016959, 192677707, 8386138845,
    475652209, 301416726, 375939130, 1043058763, 339215472
]  

USER_TO_NAME = {
    283970723: "Матвей", 183590516: "Сергей JR", 146770254: "Надя",
    1129899475: "Маша", 146921711: "Антон", 333946991: "Ксю",
    402914102: "Таня", 816016959: "Ваня", 192677707: "Алена",
    8386138845: "Валера Турский", 475652209: "Егор Карев",
    301416726: "Кузя", 375939130: "Левик", 1043058763: "Валера Родак",
    339215472: "Лобос",
}

DATE_FORMATS = [
    "%d.%m.%Y %H:%M", "%d.%m.%y %H:%M", "%d.%m.%Y", "%d.%m.%y", "%d.%m"
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== Время турнира =====

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

LOCK_BEFORE_MATCH = timedelta(minutes=16)

def get_moscow_now():
    return datetime.now(MOSCOW_TZ)

def is_match_open(match_date):
    """
    Можно ли ещё ставить/редактировать прогноз.
    Закрываем за 16 минут до начала матча.
    """
    if match_date.tzinfo is None:
        match_date = match_date.replace(tzinfo=MOSCOW_TZ)
    return get_moscow_now() < match_date - LOCK_BEFORE_MATCH

def get_column_letter(col_num):
    """
    1 -> A
    2 -> B
    ...
    26 -> Z
    27 -> AA
    28 -> AB
    """

    result = ""

    while col_num > 0:
        col_num, remainder = divmod(col_num - 1, 26)
        result = chr(65 + remainder) + result

    return result

async def launch_forecast_for_user(user_id, send_func):
    try:
        service = get_service()

        today = get_moscow_now().date()

        target_dates = [
            today,
            today + timedelta(days=1),
            today + timedelta(days=2)
        ]

        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=RANGE
        ).execute()

        rows = result.get('values', [])

        if len(rows) < 2:
            await send_func(
                "❌ Не удалось получить данные матчей."
            )
            return

        header_row = rows[0]

        player_name = USER_TO_NAME.get(user_id)

        if not player_name:
            await send_func(
                "❌ Вы не зарегистрированы в турнире."
            )
            return

        try:
            col_idx = header_row.index(player_name)
        except ValueError:
            await send_func(
                "❌ Ваш столбец не найден в таблице."
            )
            return

        pending_matches = []

        for i, row in enumerate(rows[1:], start=2):

            if len(row) < 5:
                continue

            date_str = row[1] if len(row) > 1 else ""
            home = row[2].strip() if len(row) > 2 else ""
            away = row[4].strip() if len(row) > 4 else ""

            if not date_str or not home or not away:
                continue

            match_date = parse_sheet_date(date_str)

            if not match_date:
                continue

            if match_date.date() not in target_dates:
                continue

            if not is_match_open(match_date):
                continue

            if len(row) <= col_idx or not str(row[col_idx]).strip():

                pending_matches.append({
                    'row_index': i,
                    'date': date_str,
                    'home': home,
                    'away': away,
                    'raw_row': row
                })

        if not pending_matches:

            await send_func(
                "✅ Все доступные матчи уже спрогнозированы."
            )
            return

        PENDING_MATCHES[user_id] = {
            'match_queue': pending_matches,
            'current_index': 0,
            'sheet_name': SHEET_NAME
        }

        current_match = pending_matches[0]

        await send_func(
            f"⚽ Найдено матчей без прогноза: {len(pending_matches)}\n\n"
            f"📅 {current_match['date']}\n"
            f"🏟 {current_match['home']} vs {current_match['away']}\n\n"
            f"Введите прогноз:\n"
            f"2:1"
        )

    except Exception as e:
        logger.error(f"Ошибка запуска постановки ставок: {e}")

        await send_func(
            "❌ Ошибка при поиске матчей."
        )

def get_service():
    try:
        creds = Credentials.from_service_account_file('credentials.json')
        return build('sheets', 'v4', credentials=creds)
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Google Sheets: {e}")
        raise

def parse_sheet_date(date_str):
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)

            return dt.replace(tzinfo=MOSCOW_TZ)
        
        except ValueError:
            continue
    logger.warning(f"Не удалось распарсить дату: {date_str}")
    return None

# === 🏠 ГЛАВНОЕ МЕНЮ ===
def get_main_keyboard():
    """Создает постоянную навигационную клавиатуру"""
    keyboard = [
        [
            KeyboardButton("📋 Мои ставки"),      
            KeyboardButton("⚽ Поставить ставки")   
        ],
        [
            KeyboardButton("✏️ Изменить ставку"),  
            KeyboardButton("🏆 Таблица лидеров")     
        ],
        [
            KeyboardButton("📊 Моя статистика"),      
            KeyboardButton("🌍 Все прогнозы")  
        ],
        [      
            KeyboardButton("📈 Полная статистика")  
        ]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    
    welcome_text = (
        f"👋 Привет, {user_name}!\n\n"
        "Я твой помощник для прогнозов на ЧМ.\n\n"
        "Используй кнопки ниже для навигации:\n"
        "• 📋 <b>/prognoz</b> — твои текущие ставки\n"
        "• ✏️ <b>/noviischet</b> — изменить прогноз\n"
        "• 🏆 <b>/standings</b> — таблица лидеров\n"
        "• 📊 <b>/mystats</b> — твоя личная статистика\n\n"
        "Выбери действие в меню ниже 👇"
    )
    
    await update.message.reply_text(
        text=welcome_text,
        reply_markup=get_main_keyboard(),
        parse_mode='HTML'
    )

PENDING_MATCHES = {}
LAST_REMINDERS = set()
DIGEST_SENT_DATES = set()  # даты, за которые уже отправлен дайджест

async def handle_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in PENDING_MATCHES:
        await update.message.reply_text("❌ Нет активного матча. Подожди напоминание от бота.", reply_markup=get_main_keyboard())
        return

    text = update.message.text.strip()
    score_match = re.fullmatch(r'^\s*(0|[1-9]\d*)\s*:\s*(0|[1-9]\d*)\s*$', text)

    if not score_match:
        await update.message.reply_text("❌ Напиши счёт в формате: `2 : 1` или `3:0` (можно без пробелов)", reply_markup=get_main_keyboard())
        return

    score_for_sheet = f"{score_match.group(1)} : {score_match.group(2)}"
    player_name = USER_TO_NAME.get(user_id, f"User{user_id}")

    queue_data = PENDING_MATCHES[user_id]
    context.user_data['match_queue'] = queue_data['match_queue']
    context.user_data['current_index'] = queue_data['current_index']

    try:
        service = get_service()
        sheet_name = queue_data['sheet_name']
        header_result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=f"{sheet_name}!1:1").execute()
        header_row = header_result.get('values', [[]])[0]
        
        try:
            col_idx = header_row.index(player_name)
        except ValueError:
            await update.message.reply_text(f"❌ Столбец '{player_name}' не найден в таблице.", reply_markup=get_main_keyboard())
            return

        current_match = context.user_data['match_queue'][context.user_data['current_index']]
        cell = f"{get_column_letter(col_idx + 1)}{current_match['row_index']}"

        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{sheet_name}!{cell}",
            valueInputOption='RAW',
            body={'values': [[score_for_sheet]]}
        ).execute()

        await update.message.reply_text(
            f"✅ Прогноз принят!\n"
            f"📌 {player_name}: {score_for_sheet}\n"
            f"🏟 {current_match['home']} vs {current_match['away']} ({current_match['date']})",
            reply_markup=get_main_keyboard()
        )

        next_index = context.user_data['current_index'] + 1
        total = len(context.user_data['match_queue'])

        if next_index >= total:
            await update.message.reply_text("🎉 Все прогнозы отправлены! Спасибо!", reply_markup=get_main_keyboard())
            context.user_data['match_queue'] = None
            context.user_data['current_index'] = 0
            if user_id in PENDING_MATCHES: del PENDING_MATCHES[user_id]
        else:
            next_match = context.user_data['match_queue'][next_index]
            await update.message.reply_text(
                f"📢 Матч {next_index + 1}/{total}: {next_match['date']}\n\n"
                f"🏟 {next_match['home']} vs {next_match['away']}\n\n"
                f"Пришли прогноз в формате:\n`2:1`",
                reply_markup=get_main_keyboard()
            )
            context.user_data['current_index'] = next_index
            PENDING_MATCHES[user_id]['current_index'] = next_index

    except Exception as e:
        logger.error(f"❌ Ошибка при записи: {e}")
        await update.message.reply_text("❌ Ошибка при сохранении. Попробуй позже.", reply_markup=get_main_keyboard())

async def send_reminders(app: Application):
    logger.info("🔍 Поиск матчей на ближайшие 3 дня...")
    try:
        service = get_service()
        today = get_moscow_now().date()  # ✅ fix: было datetime.now()
        target_dates = [today, today + timedelta(days=1), today + timedelta(days=2)]

        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE).execute()
        rows = result.get('values', [])
        if len(rows) < 2: return

        header_row = rows[0]
        for user_id in AUTHORIZED_USERS:
            if user_id in PENDING_MATCHES: continue
            
            player_name = USER_TO_NAME.get(user_id)
            if not player_name: continue

            try:
                col_idx = header_row.index(player_name)
            except ValueError: continue

            matches_for_days = []
            for i, row in enumerate(rows[1:], start=2):
                if len(row) < 5: continue
                date_str = row[1] if len(row) > 1 else ""
                home = row[2].strip() if len(row) > 2 else ""
                away = row[4].strip() if len(row) > 4 else ""
                if not date_str or not home or not away: continue
                
                match_date = parse_sheet_date(date_str)
                if not match_date: continue

                if not is_match_open(match_date):  # ✅ fix: фильтруем закрытые матчи
                    continue
                
                if match_date.date() in target_dates:
                    matches_for_days.append({'row_index': i, 'date': date_str, 'home': home, 'away': away, 'raw_row': row})

            if not matches_for_days: continue

            pending_matches = [m for m in matches_for_days if len(m['raw_row']) <= col_idx or not str(m['raw_row'][col_idx]).strip()]
            if not pending_matches: continue

            current_match = pending_matches[0]
            message = f"📢 Матч на ближайшие дни:\n\n📅 {current_match['date']}\n🏟 {current_match['home']} vs {current_match['away']}\n\nПришли прогноз в формате:\n`2:1`"
            
            try:
                await app.bot.send_message(chat_id=user_id, text=message)
                PENDING_MATCHES[user_id] = {'match_queue': pending_matches, 'current_index': 0, 'sheet_name': SHEET_NAME}
            except Exception as e:
                logger.warning(f"⚠️ Не удалось отправить сообщение пользователю {user_id}: {e}")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при рассылке: {e}")

async def send_last_reminder(app: Application):
    logger.info("⏰ Проверка напоминаний за 5 часов...")

    try:
        service = get_service()

        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=RANGE
        ).execute()

        rows = result.get('values', [])

        if len(rows) < 2:
            return

        header_row = rows[0]

        now = get_moscow_now()

        for user_id in AUTHORIZED_USERS:

            player_name = USER_TO_NAME.get(user_id)

            if not player_name:
                continue

            try:
                col_idx = header_row.index(player_name)
            except ValueError:
                continue

            for i, row in enumerate(rows[1:], start=2):

                if len(row) < 5:
                    continue

                date_str = row[1] if len(row) > 1 else ""
                home = row[2].strip() if len(row) > 2 else ""
                away = row[4].strip() if len(row) > 4 else ""

                if not date_str or not home or not away:
                    continue

                match_date = parse_sheet_date(date_str)

                if not match_date:
                    continue

                if match_date.tzinfo is None:
                    match_date = match_date.replace(
                        tzinfo=MOSCOW_TZ
                    )

                time_left = match_date - now

                # до 5ч00м
                if time_left > timedelta(hours=5):
                    continue
                if time_left <= timedelta():
                    continue

                reminder_key = (user_id, i)

                if reminder_key in LAST_REMINDERS:
                    continue

                score_exists = (
                    len(row) > col_idx
                    and str(row[col_idx]).strip()
                )

                if score_exists:
                    continue

                try:

                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "⚽ Поставить ставки",
                                callback_data="start_forecast"
                            )
                        ]
                    ])

                    await app.bot.send_message(
                        chat_id=user_id,
                        text=(
                            "⏰ Напоминание!\n\n"
                            f"Через 5 часов начинается матч:\n\n"
                            f"🏟 {home} vs {away}\n"
                            f"📅 {date_str}\n\n"
                            "Не забудьте поставить прогноз."
                        ),
                        reply_markup=keyboard
                    )

                    LAST_REMINDERS.add(reminder_key)

                except Exception as e:
                    logger.warning(
                        f"Не удалось отправить напоминание "
                        f"{user_id}: {e}"
                    )

    except Exception as e:
        logger.error(
            f"Ошибка send_last_reminder: {e}"
        )

# === КОМАНДА /table ===
async def show_table(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        service = get_service()
        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE).execute()
        rows = result.get('values', [])
        if len(rows) < 2:
            await update.message.reply_text("📊 Таблица пуста", reply_markup=get_main_keyboard())
            return
        
        header_row = rows[0]
        today = get_moscow_now().date()  # ✅ fix: было datetime.now()
        target_dates = [today, today + timedelta(days=1), today + timedelta(days=2)]
        
        upcoming_matches = []
        for i, row in enumerate(rows[1:], start=2):
            if len(row) < 5: continue
            date_str = row[1] if len(row) > 1 else ""
            home = row[2].strip() if len(row) > 2 else ""
            away = row[4].strip() if len(row) > 4 else ""
            if not date_str or not home or not away: continue
            
            match_date = parse_sheet_date(date_str)
            if not match_date: continue
            if match_date.date() in target_dates:
                upcoming_matches.append({'row': row, 'date': date_str, 'home': home, 'away': away})
        
        if not upcoming_matches:
            await update.message.reply_text("📅 Нет матчей на ближайшие 3 дня", reply_markup=get_main_keyboard())
            return
        
        message = "🏆 **ТАБЛИЦА ПРОГНОЗОВ** 🏆\n\n"
        for match in upcoming_matches[:5]:
            message += f"📅 **{match['date']}**\n🏟 {match['home']} vs {match['away']}\n"
            predictions = []
            for col_idx in range(5, len(header_row)):
                p_name = header_row[col_idx]
                if col_idx < len(match['row']):
                    pred = match['row'][col_idx].strip()
                    if pred: predictions.append(f"{p_name}: {pred}")
            
            if predictions:
                message += "📊 Прогнозы:\n"
                for pred in predictions: message += f"  • {pred}\n"
            else:
                message += "⏳ Прогнозов пока нет\n"
            message += "\n"
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f" Ошибка при показе таблицы: {e}")
        await update.message.reply_text("❌ Не удалось загрузить таблицу.", reply_markup=get_main_keyboard())

# === КОМАНДА /standings ===
async def show_standings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        service = get_service()
        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="Таблица ЧМ!A2:B16").execute()
        rows = result.get('values', [])
        if not rows:
            await update.message.reply_text("📊 Таблица пуста", reply_markup=get_main_keyboard())
            return
        
        message = "🏆 **ТУРНИРНАЯ ТАБЛИЦА** 🏆\n\n *Место* | *Участник* | *Очки*\n" + "─" * 35 + "\n"
        standings = []
        for row in rows:
            if len(row) >= 2:
                name = row[0].strip()
                score = row[1].strip()
                try: score_num = int(score) if score else 0
                except: score_num = 0
                standings.append((name, score, score_num))
        
        standings.sort(key=lambda x: x[2], reverse=True)
        for place, (name, score, _) in enumerate(standings, start=1):
            medal = "🥇" if place == 1 else ("🥈" if place == 2 else ("🥉" if place == 3 else ""))
            message += f"{medal} *{place}.* | {name} | {score}\n"
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"❌ Ошибка при показе таблицы: {e}")
        await update.message.reply_text("❌ Не удалось загрузить таблицу.", reply_markup=get_main_keyboard())

# === КОМАНДА /stats ===
async def show_detailed_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        service = get_service()
        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="Таблица ЧМ!A19:F35").execute()
        rows = result.get('values', [])
        if not rows:
            await update.message.reply_text("📊 Статистика пуста", reply_markup=get_main_keyboard())
            return
        
        message = "📊 **ПОДРОБНАЯ СТАТИСТИКА** \n\n"
        for row in rows[1:]:
            if len(row) >= 6 and row[0].strip():
                name = row[0].strip()
                forecasts = row[1].strip()
                correct_score = row[2].strip()
                diff = row[3].strip()
                outcome = row[4].strip()
                points = row[5].strip()
                
                message += f"👤 **{name}**\n"
                message += f"    Прогнозов: {forecasts}\n"
                message += f"   🎯 Точный счет: {correct_score}\n"
                message += f"   📈 Разница: {diff}\n"
                message += f"   ✅ Исход: {outcome}\n"
                message += f"   🏆 Очки: {points}\n\n"
        
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"❌ Ошибка при показе статистики: {e}")
        await update.message.reply_text(" Не удалось загрузить статистику.", reply_markup=get_main_keyboard())

# === НОВАЯ КОМАНДА /mystats ===
async def show_my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    player_name = USER_TO_NAME.get(user_id)
    
    if not player_name:
        await update.message.reply_text("❌ Вас нет в списке участников.", reply_markup=get_main_keyboard())
        return

    try:
        service = get_service()
        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="Таблица ЧМ!A19:F35").execute()
        rows = result.get('values', [])
        
        my_stats = None
        for row in rows[1:]:
            if len(row) >= 6 and row[0].strip() == player_name:
                my_stats = row
                break
        
        if not my_stats:
            await update.message.reply_text("❌ Статистика не найдена.", reply_markup=get_main_keyboard())
            return

        forecasts = my_stats[1].strip()
        correct_score = my_stats[2].strip()
        diff = my_stats[3].strip()
        outcome = my_stats[4].strip()
        points = my_stats[5].strip()

        standings_result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="Таблица ЧМ!A2:B16").execute()
        standings_rows = standings_result.get('values', [])
        
        my_place = "-"
        sorted_standings = []
        for r in standings_rows:
            if len(r) >= 2:
                try: s = int(r[1]) if r[1] else 0
                except: s = 0
                sorted_standings.append((r[0].strip(), s))
        
        sorted_standings.sort(key=lambda x: x[1], reverse=True)
        for i, (name, score) in enumerate(sorted_standings, start=1):
            if name == player_name:
                my_place = i
                break

        accuracy = "0%"
        try:
            total = int(forecasts) if forecasts else 0
            exact = int(correct_score) if correct_score else 0
            if total > 0:
                accuracy = f"{round(exact / total * 100)}%"
        except: pass

        message = f"👤 **Ваша статистика: {player_name}**\n\n"
        message += f"🏆 Место в таблице: **{my_place}**\n"
        message += f"📋 Всего прогнозов: **{forecasts}**\n"
        message += f"🎯 Точных попаданий: **{correct_score}** ({accuracy})\n"
        message += f"📈 Средняя разница: **{diff}**\n"
        message += f"✅ Угаданных исходов: **{outcome}**\n"
        message += f"💰 Текущие очки: **{points}**"

        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=get_main_keyboard())

    except Exception as e:
        logger.error(f"❌ Ошибка при показе личной статистики: {e}")
        await update.message.reply_text("❌ Не удалось загрузить вашу статистику.", reply_markup=get_main_keyboard())

async def start_forecast_manually(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    await launch_forecast_for_user(
        user_id,
        update.message.reply_text
    )
# === НОВАЯ КОМАНДА /prognoz ===
async def show_my_prognoz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    player_name = USER_TO_NAME.get(user_id)

    if not player_name:
        await update.message.reply_text(
            "❌ Вас нет в списке участников.",
            reply_markup=get_main_keyboard()
        )
        return

    try:
        service = get_service()

        today = get_moscow_now().date()

        target_dates = [
            today,
            today + timedelta(days=1),
            today + timedelta(days=2)
        ]

        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=RANGE
        ).execute()

        rows = result.get('values', [])

        if len(rows) < 2:
            await update.message.reply_text(
                "📊 Таблица пуста",
                reply_markup=get_main_keyboard()
            )
            return

        header_row = rows[0]

        try:
            col_idx = header_row.index(player_name)
        except ValueError:
            await update.message.reply_text(
                f"❌ Столбец '{player_name}' не найден.",
                reply_markup=get_main_keyboard()
            )
            return

        filled_matches = []
        empty_matches = []

        for row in rows[1:]:

            if len(row) < 5:
                continue

            date_str = row[1] if len(row) > 1 else ""
            home = row[2].strip() if len(row) > 2 else ""
            away = row[4].strip() if len(row) > 4 else ""

            if not date_str or not home or not away:
                continue

            match_date = parse_sheet_date(date_str)

            if not match_date:
                continue

            if match_date.date() not in target_dates:
                continue

            score = ""

            if len(row) > col_idx:
                score = str(row[col_idx]).strip()

            if score:

                filled_matches.append({
                    "date": date_str,
                    "home": home,
                    "away": away,
                    "score": score
                })

            elif is_match_open(match_date):

                empty_matches.append({
                    "date": date_str,
                    "home": home,
                    "away": away
                })

        if not filled_matches and not empty_matches:

            await update.message.reply_text(
                "📅 На ближайшие 3 дня матчей нет.",
                reply_markup=get_main_keyboard()
            )
            return

        message = f"🎯 Ваши ставки ({player_name})\n\n"

        if filled_matches:

            message += "✅ Поставленные прогнозы:\n\n"

            for m in filled_matches:

                message += (
                    f"📅 {m['date']}\n"
                    f"🏟 {m['home']} vs {m['away']}\n"
                    f"💰 Прогноз: {m['score']}\n\n"
                )

        if empty_matches:

            message += "\n❌ Не поставлены:\n\n"

            for m in empty_matches:

                message += (
                    f"📅 {m['date']}\n"
                    f"🏟 {m['home']} vs {m['away']}\n"
                    f"💰 Прогноз отсутствует\n\n"
                )

            message += (
                "\n⚽ Чтобы заполнить пропущенные матчи,\n"
                "нажмите кнопку «Поставить ставки»."
            )
        if empty_matches:

            keyboard = [
                [
                    InlineKeyboardButton(
                        "⚽ Поставить ставки",
                        callback_data="start_forecast"
                    )
                ]
            ]

            await update.message.reply_text(
                message,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                message,
                reply_markup=get_main_keyboard()    
        )

    except Exception as e:
        logger.error(f"❌ Ошибка при показе моих ставок: {e}")

        await update.message.reply_text(
            "❌ Не удалось загрузить данные.",
            reply_markup=get_main_keyboard()
        )

async def start_forecast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    await launch_forecast_for_user(
        user_id,
        query.message.reply_text
    )

# === ОБНОВЛЕННЫЙ /noviischet С КНОПКАМИ И МЕНЮ ===
CHOOSING_MATCH, ENTERING_NEW_SCORE = range(2)

async def noviischet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    player_name = USER_TO_NAME.get(user_id)
    if not player_name:
        await update.message.reply_text("❌ Вас нет в списке участников.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    try:
        service = get_service()
        today = get_moscow_now().date()  # ✅ fix: было datetime.now()
        target_dates = [today, today + timedelta(days=1), today + timedelta(days=2)]

        result = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=RANGE).execute()
        rows = result.get('values', [])
        if len(rows) < 2:
            await update.message.reply_text("📊 Таблица пуста", reply_markup=get_main_keyboard())
            return ConversationHandler.END

        header_row = rows[0]
        try: col_idx = header_row.index(player_name)
        except ValueError:
            await update.message.reply_text(f"❌ Столбец '{player_name}' не найден.", reply_markup=get_main_keyboard())
            return ConversationHandler.END

        editable_matches = []
        for i, row in enumerate(rows[1:], start=2):
            if len(row) < 5: continue
            date_str = row[1] if len(row) > 1 else ""
            home = row[2].strip() if len(row) > 2 else ""
            away = row[4].strip() if len(row) > 4 else ""
            if not date_str or not home or not away: continue

            match_date = parse_sheet_date(date_str)
            if not match_date: continue
            if not is_match_open(match_date):  # ✅ fix: нельзя редактировать закрытый матч
                continue
            if match_date.date() in target_dates:
                if len(row) > col_idx and str(row[col_idx]).strip():
                    editable_matches.append({
                        'id': i,  # ✅ fix: row_index как стабильный id вместо len()
                        'date': date_str, 'home': home, 'away': away,
                        'my_score': row[col_idx].strip(),
                        'row_index': i, 'col_idx': col_idx
                    })

        if not editable_matches:
            await update.message.reply_text("⏳ У вас нет ставок на ближайшие 3 дня, которые можно изменить.", reply_markup=get_main_keyboard())
            return ConversationHandler.END

        context.user_data['editable_matches'] = editable_matches
        
        keyboard = []
        for m in editable_matches:
            btn_text = f"{m['home']} vs {m['away']} ({m['my_score']})"
            keyboard.append([
                InlineKeyboardButton(
                    btn_text,
                    callback_data=f"edit_{m['id']}"
                )
            ])

        
        await update.message.reply_text(
            "✏️ **Изменение ставки**\n\nВыберите матч, который хотите изменить:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return CHOOSING_MATCH

    except Exception as e:
        logger.error(f"❌ Ошибка в noviischet_start: {e}")
        await update.message.reply_text("❌ Произошла ошибка.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

async def choose_match_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not query.data.startswith("edit_"):
        return CHOOSING_MATCH
    
    match_id = int(query.data.split("_")[1])
    editable_matches = context.user_data.get('editable_matches', [])
    
    selected = None
    for m in editable_matches:
        if m['id'] == match_id:
            selected = m
            break

    if not selected:
        await query.edit_message_text("❌ Ошибка выбора матча. Попробуйте заново.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    context.user_data['selected_match'] = selected
    
    await query.edit_message_text(
        f"✏️ Вы выбрали матч:\n\n"
        f"🏟 {selected['home']} vs {selected['away']}\n"
        f"📅 {selected['date']}\n"
        f"📊 Текущий прогноз: {selected['my_score']}\n\n"
        f"Введите новый счёт в формате:\n"
        f"`2:1`",
        parse_mode='Markdown'
    )
    return ENTERING_NEW_SCORE

async def enter_new_score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    score_match = re.fullmatch(r'^\s*(0|[1-9]\d*)\s*:\s*(0|[1-9]\d*)\s*$', text)

    if not score_match:
        await update.message.reply_text(
            "❌ Неверный формат или недопустимый счет.\n"
            "Правила:\n"
            "• Нельзя писать ведущие нули (например, '01' — пишите '1')\n"
            "• Формат: `2:1` или `10:0`\n"
            f"\nВы написали: `{text}`",
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )
        return ENTERING_NEW_SCORE

    home_goals = int(score_match.group(1))
    away_goals = int(score_match.group(2))
    new_score = f"{home_goals} : {away_goals}"
    
    selected = context.user_data.get('selected_match')
    if not selected:
        await update.message.reply_text("❌ Ошибка: матч не выбран. Попробуйте /noviischet заново.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    try:
        service = get_service()
        col_letter = get_column_letter(selected['col_idx'] + 1)
        cell = f"{col_letter}{selected['row_index']}"

        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_NAME}!{cell}",
            valueInputOption='RAW',
            body={'values': [[new_score]]}
        ).execute()

        await update.message.reply_text(
            f"✅ Прогноз успешно изменен!\n\n"
            f" {selected['home']} vs {selected['away']}\n"
            f"Было: {selected['my_score']}\n"
            f"Стало: **{new_score}**",
            parse_mode='Markdown',
            reply_markup=get_main_keyboard()
        )
        context.user_data.pop('editable_matches', None)
        context.user_data.pop('selected_match', None)
        
    except Exception as e:
        logger.error(f"❌ Ошибка при обновлении счета: {e}")
        await update.message.reply_text("❌ Не удалось сохранить изменение. Попробуйте позже.", reply_markup=get_main_keyboard())

    return ConversationHandler.END

async def cancel_noviischet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Изменение ставки отменено.", reply_markup=get_main_keyboard())
    context.user_data.pop('editable_matches', None)
    context.user_data.pop('selected_match', None)
    return ConversationHandler.END


async def send_daily_digest(app: Application):
    """
    Отправляет дайджест дня через час после последнего матча.
    Показывает очки за день, угаданные исходы, разницы и итоговую таблицу.
    """
    now = get_moscow_now()
    today = now.date()

    if today in DIGEST_SENT_DATES:
        return

    try:
        service = get_service()

        # Проверяем есть ли матчи сегодня и прошёл ли час после последнего
        result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range=RANGE
        ).execute()
        rows = result.get("values", [])
        if len(rows) < 2:
            return

        todays_matches = []
        for i, row in enumerate(rows[1:], start=2):
            if len(row) < 5:
                continue
            date_str = row[1] if len(row) > 1 else ""
            if not date_str:
                continue
            match_date = parse_sheet_date(date_str)
            if not match_date:
                continue
            if match_date.date() == today:
                todays_matches.append(match_date)

        if not todays_matches:
            return

        last_match_time = max(todays_matches)
        if now < last_match_time + timedelta(hours=1):
            return

        # Берём статистику из таблицы ЧМ
        stats_result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Таблица ЧМ!A19:F35"
        ).execute()
        stats_rows = stats_result.get("values", [])

        # Берём итоговую таблицу очков
        standings_result = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Таблица ЧМ!A2:B16"
        ).execute()
        standings_rows = standings_result.get("values", [])

        standings = []
        for r in standings_rows:
            if len(r) >= 2 and r[0].strip():
                try:
                    standings.append((r[0].strip(), int(r[1]) if r[1] else 0))
                except Exception:
                    standings.append((r[0].strip(), 0))
        standings.sort(key=lambda x: x[1], reverse=True)

        # Считаем очки за день: total_points - вчерашние очки
        # Поскольку у нас нет вчерашних очков напрямую,
        # показываем из статистики: прогнозы, точные, разницы, исходы
        nl = "\n"
        today_str = today.strftime("%d.%m.%Y")
        message = "📣 *Итоги дня — " + today_str + "*" + nl + nl

        # Статистика по игрокам
        if len(stats_rows) > 1:
            message += "📊 *Статистика участников:*" + nl
            for row in stats_rows[1:]:
                if len(row) >= 6 and row[0].strip():
                    name = row[0].strip()
                    forecasts = row[1].strip() if row[1] else "0"
                    exact = row[2].strip() if row[2] else "0"
                    diff = row[3].strip() if row[3] else "0"
                    outcome = row[4].strip() if row[4] else "0"
                    pts = row[5].strip() if row[5] else "0"
                    message += (nl + "👤 *" + name + "* — " + pts + " очков" + nl)
                    message += "  🎯 Точный счёт: " + exact + nl
                    message += "  📈 Разница: " + diff + nl
                    message += "  ✅ Угаданных исходов: " + outcome + nl
            message += nl

        # Итоговая таблица
        message += "🏆 *Таблица лидеров:*" + nl
        medals = ["🥇", "🥈", "🥉"]
        for place, (name, pts) in enumerate(standings, start=1):
            medal = medals[place - 1] if place <= 3 else str(place) + "."
            message += medal + " " + name + " — " + str(pts) + " очков" + nl

        for user_id in AUTHORIZED_USERS:
            try:
                await app.bot.send_message(
                    chat_id=user_id, text=message, parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning("Не удалось отправить дайджест %s: %s", user_id, e)

        DIGEST_SENT_DATES.add(today)
        logger.info("Дайджест за %s отправлен", today)

    except Exception as e:
        logger.error("Ошибка send_daily_digest: %s", e)


def main():
    if not TOKEN or not SPREADSHEET_ID:
        print(" Ошибка: не задан TELEGRAM_TOKEN или SPREADSHEET_ID")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("table", show_table))
    app.add_handler(CommandHandler("standings", show_standings))
    app.add_handler(CommandHandler("top", show_standings))
    app.add_handler(CommandHandler("stats", show_detailed_stats))
    app.add_handler(CommandHandler("detailed", show_detailed_stats))
    app.add_handler(CommandHandler("prognoz", show_my_prognoz))
    app.add_handler(CommandHandler("mystats", show_my_stats))
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("noviischet", noviischet_start),
            MessageHandler(
                filters.Regex("^✏️ Изменить ставку$"),
                noviischet_start
            ),
        ],
        states={
            CHOOSING_MATCH: [
                CallbackQueryHandler(choose_match_callback)
            ],
            ENTERING_NEW_SCORE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    enter_new_score
                )
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_noviischet),
            CallbackQueryHandler(start_forecast_callback, pattern="^start_forecast$"),
        ],
        allow_reentry=True
    )
    app.add_handler(conv_handler)

    app.add_handler(
        MessageHandler(filters.Regex("^📋 Мои ставки$"), show_my_prognoz)
    )
    app.add_handler(
        MessageHandler(
            filters.Regex("^⚽ Поставить ставки$"),
            start_forecast_manually
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            start_forecast_callback,
            pattern="^start_forecast$"
        )
    )

    app.add_handler(
        MessageHandler(filters.Regex("^🏆 Таблица лидеров$"), show_standings)
    )

    app.add_handler(
        MessageHandler(filters.Regex("^📊 Моя статистика$"), show_my_stats)
    )

    app.add_handler(
        MessageHandler(filters.Regex("^🌍 Все прогнозы$"), show_table)
    )

    app.add_handler(
        MessageHandler(filters.Regex("^📈 Полная статистика$"), show_detailed_stats)
    )

    async def forecast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in PENDING_MATCHES:
            await update.message.reply_text("❌ Нет активного матча. Подожди напоминание от бота.", reply_markup=get_main_keyboard())
            return
        queue_data = PENDING_MATCHES[user_id]
        context.user_data['match_queue'] = queue_data['match_queue']
        context.user_data['current_index'] = queue_data['current_index']
        await handle_forecast(update, context)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forecast_handler))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_reminders, trigger=CronTrigger(hour=13, minute=44), args=[app])
    scheduler.add_job(send_last_reminder,trigger='interval',minutes=2,args=[app])
    scheduler.add_job(send_daily_digest, trigger='interval', minutes=5, args=[app])
    
    async def post_init(application: Application):
        scheduler.start()
        logger.info("✅ Планировщик запущен")

    app.post_init = post_init
    print("✅ Бот запущен. Проверка матчей — каждый день в 13:44.")
    app.run_polling()

if __name__ == '__main__':
    main()