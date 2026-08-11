from __future__ import annotations
import asyncio
import logging
import random
import re
import json
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler
)

from ai_personas import PERSONAS

# Imports for Meal Planner
from meal_planner.menu_generator import generate_menu
from meal_planner.shopping_list_aggregator import aggregate_shopping_list

logger = logging.getLogger("handlers")

# --- States for Meal Planner ---
ASK_DAYS, ASK_SHOPPING_LIST = range(10, 12) # Using a higher range to avoid conflicts

# --- Meal Planner Functions ---
def load_recipes():
    """Загружает рецепты из JSON файла."""
    try:
        with open('meal_planner/recipes.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("Файл meal_planner/recipes.json не найден!")
        return []
    except json.JSONDecodeError:
        logger.error("Ошибка декодирования JSON в файле meal_planner/recipes.json!")
        return []

async def plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог планирования и спрашивает количество дней."""
    keyboard = [
        [
            InlineKeyboardButton("3 дня", callback_data="plan_3"),
            InlineKeyboardButton("5 дней", callback_data="plan_5"),
            InlineKeyboardButton("7 дней", callback_data="plan_7"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("На сколько дней составить меню?", reply_markup=reply_markup)
    return ASK_DAYS

async def ask_days_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор количества дней, генерирует меню и предлагает создать список покупок."""
    query = update.callback_query
    await query.answer()
    
    days = int(query.data.split('_')[1])
    recipes = context.bot_data.get('recipes', [])
    
    menu, error = generate_menu(recipes, days)
    
    if error:
        await query.edit_message_text(text=error)
        return ConversationHandler.END

    context.user_data['menu'] = menu

    response_text = f"✅ **Ваш план меню на {days} дней:**\n\n"
    for day, recipe in menu.items():
        response_text += f"**{day}:** {recipe['name']}\n"
    
    await query.edit_message_text(text=response_text, parse_mode=ParseMode.MARKDOWN)

    keyboard = [
        [InlineKeyboardButton("🛒 Сгенерировать список покупок", callback_data="generate_list")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text("Теперь я могу создать список покупок для этого меню.", reply_markup=reply_markup)
    return ASK_SHOPPING_LIST

async def shopping_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Генерирует и выводит итоговый список покупок."""
    query = update.callback_query
    await query.answer()

    menu = context.user_data.get('menu')
    if not menu:
        await query.edit_message_text(text="Произошла ошибка: меню не найдено. Начните заново с /plan.")
        return ConversationHandler.END
        
    shopping_list, error = aggregate_shopping_list(menu)

    if error:
        await query.edit_message_text(text=error)
        return ConversationHandler.END

    response_text = "🛒 **Ваш список покупок:**\n\n"
    for product, details in shopping_list.items():
        response_text += f"• **{product}**: {', '.join(details)}\n"

    await query.edit_message_text(text=response_text, parse_mode=ParseMode.MARKDOWN)
    
    context.user_data.clear()
    return ConversationHandler.END

async def plan_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет диалог планирования."""
    await update.message.reply_text("Планирование отменено.")
    context.user_data.clear()
    return ConversationHandler.END

# --- End of Meal Planner Functions ---


# Load genres data for /toprock command
try:
    with open(Path(__file__).parent / "genres.json", "r", encoding="utf-8") as f:
        GENRES_DATA = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    GENRES_DATA = {}

GREETINGS = {
    "default": ["Привет! Я снова я. 🎧", "Режим по умолчанию. Погнали!"],
    "toxic": ["Ну че, переключил? Теперь терпи.", "Режим токсика активирован. 🙄"],
    "gop": ["Здарова, бродяга! Че каво?", "Вечер в хату."],
    "chill": ["Вайб включен... 🌌", "Расслабься..."],
    "expert": ["Рада вернуться к интеллектуальным беседам.", "Анализ музыкальных произведений запущен."],
    "standup": ["О, новые зрители! Готовьтесь к прожарке.", "Проверка микрофона... раз-два."],
    "cyberpunk": ["Система взломана. Я в сети. 🌐", "Подключение к матрице установлено. Готовь уши."],
    "anime": ["Охайо, семпай! Аврора-тян готова ставить музыку! ✨", "Уиии! Давайте веселиться! 💖"],
    "joker": ["Слышали анекдот про басиста? Потом расскажу! 🎉", "Время шуток и хорошей музыки! 😂"],
    "news": ["В эфире экстренный выпуск новостей музыки. 📰", "Сводка новостей: вы подключились. 📡"],
    "coach": ["Упал-отжался! Время качать уши! 💪", "На старт, внимание, марш! 🔥"],
    "nurse": ["Здравствуйте, на что жалуемся? Сейчас вылечим. 🩺", "Приготовьтесь, сейчас будет укол музыкой. 💉"],
    "diva": ["Я здесь, можете не аплодировать. 💅", "Дорогуши, этот эфир теперь официально роскошный. 💋"],
    "witch": ["Я вижу твое будущее... оно звучит громко. 🔮", "Духи подсказали мне включить микрофон. 🌙"],
    "teacher": ["Звонок для учителя! Сели ровно. 📏", "Открываем тетради, записываем тему урока. 📚"]
}

async def _do_play(chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, dedication: str = None):
    msg = await context.bot.send_message(chat_id, f"🔎 Ищу: *{query[:100]}*...", parse_mode=ParseMode.MARKDOWN, disable_notification=True)
    downloader = context.application.downloader
    tracks = await downloader.search(query, limit=5)

    if tracks:
        for track in tracks:
            dl_res = await downloader.download(track.identifier, track)
            if dl_res.success and dl_res.file_path:
                await msg.delete()
                try:
                    info = dl_res.track_info
                    if dedication:
                        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                        prompt = f"Ты в прямом эфире радио! Пользователь заказал трек '{info.artist} - {info.title}' и оставил послание: '{dedication}'. Сделай крутую подводку к треку и передай это послание от себя в своем уникальном стиле! Будь кратким."
                        announcement = await context.application.chat_manager.get_response(chat_id, prompt, "System")
                        if announcement: await context.bot.send_message(chat_id, f"🎙 {announcement}")

                    settings = context.application.settings
                    player_url = getattr(settings, 'PLAYER_URL', '') or getattr(settings, 'BASE_URL', '') or getattr(settings, 'WEBHOOK_URL', '').replace('/telegram', '')
                    
                    markup = None
                    if player_url:
                        if not player_url.startswith('http'): player_url = f"https://{player_url}"
                        markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Плеер", url=player_url)]])
                    
                    with open(dl_res.file_path, 'rb') as f:
                        await context.bot.send_audio(
                            chat_id=chat_id, audio=f,
                            title=info.title if info else "Track", 
                            performer=info.artist if info else "Unknown", 
                            duration=info.duration if info else 0,
                            reply_markup=markup,
                            write_timeout=300
                        )
                    return 
                except Exception as e:
                    logger.error(f"Error sending audio: {e}", exc_info=True)
                    await context.bot.send_message(chat_id, "❌ Ошибка при отправке файла.")
                    return
        await msg.edit_text("😕 Не удалось скачать трек.")
    else:
        await msg.edit_text("😕 Ничего не найдено по этому запросу.")

async def _do_radio(chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, chat_type: str | None = None, display_name: Optional[str] = None):
    effective_query = query or "random"

    # Use the provided display_name or default to the query
    final_display_name = display_name or (query if query else "Случайная волна")
    await context.bot.send_message(chat_id, f"🎧 Включаю радио-волну: *{final_display_name}*", parse_mode=ParseMode.MARKDOWN)

    radio_manager = context.bot_data['radio_manager']

    # Pass all relevant info to the manager (store task reference to prevent GC)
    task = asyncio.create_task(radio_manager.start(chat_id, effective_query, chat_type=chat_type, display_name=final_display_name))
    if not hasattr(context.application, '_bg_tasks'):
        context.application._bg_tasks = set()
    context.application._bg_tasks.add(task)
    task.add_done_callback(context.application._bg_tasks.discard)

async def _do_chat_reply(chat_id: int, text: str, user_name: str, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    chat_manager = context.application.chat_manager
    response = await chat_manager.get_response(chat_id, text, user_name)
    if response: await context.bot.send_message(chat_id, response)

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await context.bot.send_message(chat_id, "🎧 <i>Анализирую голос...</i>", parse_mode=ParseMode.HTML)

    try:
        voice_file = await update.message.voice.get_file()
        voice_bytes = await voice_file.download_as_bytearray()
        ai_manager = context.application.ai_manager
        
        transcribed_text = await ai_manager.transcribe_voice(voice_bytes)
        if not transcribed_text:
            await msg.edit_text("❌ ИИ не смог разобрать слова. Повторите четче.")
            return

        await msg.edit_text(f"🗣 <b>Вы сказали:</b> {transcribed_text}", parse_mode=ParseMode.HTML)
        
        update.effective_message.text = transcribed_text 
        await text_handler(update, context)

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await msg.edit_text("❌ Ошибка распознавания голоса.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text: return
    chat_id = update.effective_chat.id
    message_text = message.text

    quiz_manager = context.bot_data['quiz_manager']
    if quiz_manager.is_active(chat_id):
        if message_text.startswith('/'): return
        
        is_correct = await quiz_manager.process_answer(chat_id, update.effective_user.id, update.effective_user.first_name, message_text, context.bot)
        
        if not is_correct:
            try: await message.set_reaction(reaction="👎")
            except Exception: pass
        
        return

    is_private = update.effective_chat.type == ChatType.PRIVATE
    is_reply = message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id
    is_mention = (not message_text.startswith('/')) and any(m in message_text.lower() for m in ["аврора", "aurora", "бот", "dj"])

    if is_private or is_reply or is_mention:
        await _do_chat_reply(chat_id, message_text, update.effective_user.first_name, context)
        return

    ai_manager = context.application.ai_manager
    analysis = await ai_manager.analyze_message(message_text)
    intent, query = analysis.get("intent"), analysis.get("query")
    
    if intent == 'search' and query:
        if "|" in query:
            q, d = query.split("|", 1)
            await _do_play(chat_id, q.strip(), context, dedication=d.strip())
        else: await _do_play(chat_id, query, context)
    elif intent == 'radio' and query: await _do_radio(chat_id, query, context, chat_type=update.effective_chat.type)
    elif intent == 'chat': await _do_chat_reply(chat_id, message_text, update.effective_user.first_name, context)

async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quiz_mgr = context.bot_data['quiz_manager']
    task = asyncio.create_task(quiz_mgr.start_quiz(update.effective_chat.id, context.bot))
    if not hasattr(context.application, '_bg_tasks'):
        context.application._bg_tasks = set()
    context.application._bg_tasks.add(task)
    task.add_done_callback(context.application._bg_tasks.discard) 

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎧 Aurora AI DJ. Включаю радио или ищу треки. С чего начнем?")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Что найти? Введите: `/play песня | ваше послание`", parse_mode=ParseMode.MARKDOWN)
        return
    raw_query = " ".join(context.args)
    if "|" in raw_query:
        q, d = raw_query.split("|", 1)
        await _do_play(update.effective_chat.id, q.strip(), context, dedication=d.strip())
    else:
        await _do_play(update.effective_chat.id, raw_query, context)

async def radio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_radio(update.effective_chat.id, " ".join(context.args), context, chat_type=update.effective_chat.type)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    radio_manager = context.bot_data['radio_manager']
    if await radio_manager.stop(update.effective_chat.id):
        await context.bot.send_message(update.effective_chat.id, "🛑 Радио остановлено.")

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    radio_manager = context.bot_data['radio_manager']
    await radio_manager.skip(update.effective_chat.id)
    await context.bot.send_message(update.effective_chat.id, "⏭ Переключаю трек...", disable_notification=True)

async def set_genre_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = context.application.settings
    
    admin_ids_str = getattr(settings, 'ADMIN_IDS', '')
    admin_ids = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]
    admin_ids.extend(getattr(settings, 'ADMIN_ID_LIST', []))
    
    if user_id not in admin_ids:
        await update.message.reply_text("⛔️ Эта команда только для админов.")
        return

    if not context.args:
        await update.message.reply_text("🤔 Укажите жанр. Например: `/set_genre 80s rock`", parse_mode=ParseMode.MARKDOWN)
        return

    genre = " ".join(context.args)
    radio_manager = context.bot_data.get('radio_manager')
    
    if radio_manager and await radio_manager.set_genre(update.effective_chat.id, genre):
        await update.message.reply_text(f"✅ Окей, временно ставлю жанр: *{genre}*", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("🤔 Радио не запущено. Сначала включите его командой `/radio`.")

async def artist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("🤔 Укажите исполнителя. Например: `/artist Queen`", parse_mode=ParseMode.MARKDOWN)
        return

    artist = " ".join(context.args)
    radio_manager = context.bot_data.get('radio_manager')

    if radio_manager and await radio_manager.set_artist(update.effective_chat.id, artist):
        await update.message.reply_text(f"✅ Понял, сейчас будут только треки *{artist}*", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("🤔 Радио не запущено. Сначала включите его командой `/radio`.")

async def rockdance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the special RockDance playlist."""
    radio_manager = context.bot_data['radio_manager']
    chat_id = update.effective_chat.id
    await update.message.reply_text("🎸🥁 Запускаю спец-плейлист 'RockDance'!")
    
    # Use a unique query to identify the playlist mode, and the display_name must match genres.json
    await _do_radio(
        chat_id=chat_id,
        query="playlist:rockdance",
        context=context,
        display_name="RockDance",
        chat_type=update.effective_chat.type
    )

async def toprock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plays a random artist from the RockDance playlist."""
    try:
        if not GENRES_DATA:
            await update.message.reply_text("❌ Ошибка: файл жанров не загружен.")
            return

        rockdance_tracks = GENRES_DATA.get("playlists", {}).get("children", {}).get("rockdance", {}).get("tracks", [])
        if not rockdance_tracks:
            await update.message.reply_text("❌ Ошибка: плейлист 'RockDance' не найден или пуст.")
            return

        artists = {track.split('–')[0].strip() for track in rockdance_tracks}
        
        if not artists:
            await update.message.reply_text("Не удалось найти артистов в плейлисте RockDance.")
            return

        random_artist = random.choice(list(artists))
        
        await update.message.reply_text(f"🎸 Включаю случайную волну по исполнителю из 'Top Rock': *{random_artist}*", parse_mode=ParseMode.MARKDOWN)
        
        # Re-use the existing _do_radio helper function
        await _do_radio(
            chat_id=update.effective_chat.id,
            query=random_artist,
            context=context,
            display_name=f"Исполнитель: {random_artist}",
            chat_type=update.effective_chat.type
        )

    except Exception as e:
        logger.error(f"Toprock command error: {e}", exc_info=True)
        await update.message.reply_text("❌ Произошла ошибка при запуске /toprock.")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = context.application.settings
    
    admin_ids_str = getattr(settings, 'ADMIN_IDS', '')
    admin_ids = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]
    admin_ids.extend(getattr(settings, 'ADMIN_ID_LIST', []))
    
    is_admin = user_id in admin_ids

    if not is_admin:
        await update.message.reply_text(f"⛔️ Вы не админ. Ваш ID: `{user_id}`", parse_mode=ParseMode.MARKDOWN)
        return

    current_mode = await context.application.chat_manager.get_mode(update.effective_chat.id)
    
    mode_names = { 
        "default": "Эстет", "standup": "Комик", "expert": "Эксперт", 
        "gop": "Гопник", "toxic": "Токсик", "chill": "Чилл", 
        "cyberpunk": "Хакер 🌐", "anime": "Аниме 🌸", "joker": "Анекдоты 🤡", "news": "Новости 📰",
        "coach": "Тренер 💪",
        "nurse": "Медсестра 🩺",
        "diva": "Дива 💅",
        "witch": "Гадалка 🔮",
        "teacher": "Училка 📚"
    }
    
    buttons = [InlineKeyboardButton(f"{'✅ ' if mode == current_mode else ''}{mode_names.get(mode, mode)}", callback_data=f"set_mode|{mode}") for mode in PERSONAS.keys()]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="close_admin")])
    
    await context.bot.send_message(update.effective_chat.id, f"🤖 Режим AI: *{current_mode.upper()}*", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    settings = context.application.settings
    
    if query.data == "close_admin":
        await query.delete_message()
        return

    if query.data == "skip_track":
        radio_manager = context.bot_data['radio_manager']
        await radio_manager.skip(update.effective_chat.id)
        return

    if query.data.startswith("set_mode|"):
        admin_ids_str = getattr(settings, 'ADMIN_IDS', '')
        admin_ids = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]
        admin_ids.extend(getattr(settings, 'ADMIN_ID_LIST', []))
        is_admin = user_id in admin_ids

        if not is_admin:
            await query.answer("⛔️ Только для админа!", show_alert=True)
            return
            
        mode = query.data.split("|")[1]
        await context.application.chat_manager.set_mode(update.effective_chat.id, mode)
        greeting = random.choice(GREETINGS.get(mode, ["Привет!"]))
        await context.bot.send_message(update.effective_chat.id, greeting)
        await query.delete_message()

async def test_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs a diagnostic test on AI providers."""
    await update.message.reply_text("🤖 Запускаю диагностику AI-провайдеров...")
    ai_manager = context.application.ai_manager
    report = await ai_manager.test_providers()
    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows skip statistics."""
    cache = context.application.cache
    stats = await cache.hgetall("skip_stats")
    if not stats:
        await update.message.reply_text("📊 Статистика пропусков пока пуста.")
        return
    
    text = "📊 *Статистика пропусков (Skip-Analytics):*\n\n"
    for key, count in stats.items():
        text += f"- {key}: {count}\n"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def disk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks disk usage of downloads directory."""
    downloads_dir = context.application.settings.DOWNLOADS_DIR
    if not downloads_dir.exists():
        await update.message.reply_text("📁 Папка загрузок еще не создана.")
        return

    files = list(downloads_dir.glob("*.mp3"))
    total_size = sum(f.stat().st_size for f in files)
    total_size_mb = total_size / (1024 * 1024)
    
    await update.message.reply_text(
        f"💾 *Статистика диска:*\n"
        f"- Путь: `{downloads_dir}`\n"
        f"- Файлов: {len(files)}\n"
        f"- Занято: {total_size_mb:.2f} MB",
        parse_mode=ParseMode.MARKDOWN
    )

def setup_handlers(app: Application):
    # Meal Planner Handler
    meal_planner_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("plan", plan_start)],
        states={
            ASK_DAYS: [CallbackQueryHandler(ask_days_callback, pattern='^plan_')],
            ASK_SHOPPING_LIST: [CallbackQueryHandler(shopping_list_callback, pattern='^generate_list$')]
        },
        fallbacks=[CommandHandler("cancel", plan_cancel)],
        map_to_parent={
            # This ensures that if the user sends a command that is not part of the
            # meal planner conversation, the main bot handlers can still catch it.
            ConversationHandler.END: ConversationHandler.END
        }
    )
    app.add_handler(meal_planner_conv_handler)
    
    # Load recipes into bot_data
    app.bot_data['recipes'] = load_recipes()

    # Original Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("play", play_command))
    app.add_handler(CommandHandler("radio", radio_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(CommandHandler("set_genre", set_genre_command))
    app.add_handler(CommandHandler("artist", artist_command))
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("rockdance", rockdance_command))
    app.add_handler(CommandHandler("toprock", toprock_command))
    app.add_handler(CommandHandler("test_ai", test_ai_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("disk", disk_command))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
