from __future__ import annotations
import logging
import asyncio
import json
import random
import re

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters
)

from ai_personas import PERSONAS

logger = logging.getLogger("handlers")

GREETINGS = {
    "default": ["Привет! Я снова я. 🎧", "Режим по умолчанию. Погнали!"],
    "toxic": ["Ну че, переключил? Теперь терпи.", "Режим токсика активирован. 🙄"],
    "gop": ["Здарова, бродяга! Че каво?", "Вечер в хату."],
    "chill": ["Вайб включен... 🌌", "Расслабься..."],
    "expert": ["Рада вернуться к интеллектуальным беседам.", "Анализ музыкальных произведений запущен."],
    "standup": ["О, новые зрители! Готовьтесь к прожарке.", "Проверка микрофона... раз-два."],
    "cyberpunk": ["Система взломана. Я в сети. 🌐", "Подключение к матрице установлено. Готовь уши."],
    "anime": ["Охайо, семпай! Аврора-тян готова ставить музыку! ✨", "Уиии! Давайте веселиться! 💖"],
    # 🔥 НОВЫЕ ПРИВЕТСТВИЯ
    "joker": ["Слышали анекдот про басиста? Потом расскажу! 🎉", "Время шуток и хорошей музыки! 😂"],
    "news": ["В эфире экстренный выпуск новостей музыки. 📰", "Сводка новостей: вы подключились. 📡"]
}

# --- Internal Action Functions ---

async def _do_spotify_play(chat_id: int, spotify_url: str, context: ContextTypes.DEFAULT_TYPE):
    msg = await context.bot.send_message(chat_id, "🎶 Ищу трек в Spotify...", disable_notification=True)
    spotify_service = context.application.spotify_service
    dl_res = await spotify_service.download_from_url(spotify_url)
    await msg.delete()

    if dl_res.success and dl_res.file_path:
        try:
            info = dl_res.track_info
            with open(dl_res.file_path, 'rb') as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f, title=info.title, performer=info.artist)
        except Exception:
            await context.bot.send_message(chat_id, "❌ Ошибка при отправке файла.")
    else:
        await context.bot.send_message(chat_id, "😕 Не удалось скачать трек из Spotify.")

async def _do_play(chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE):
    msg = await context.bot.send_message(
        chat_id, f"🔎 Ищу: *{query[:100]}*...",
        parse_mode=ParseMode.MARKDOWN, disable_notification=True
    )

    downloader = context.application.downloader
    # ⚠️ ИЩЕМ 5 ТРЕКОВ ПРО ЗАПАС, А НЕ 1
    tracks = await downloader.search(query, limit=5)

    if tracks:
        for track in tracks:
            dl_res = await downloader.download(track.identifier, track)

            # Если трек успешно скачался и прошел лимит в 20 МБ
            if dl_res.success and dl_res.file_path:
                await msg.delete()
                try:
                    info = dl_res.track_info
                    
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
                            reply_markup=markup
                        )
                    return # УСПЕХ! ВЫХОДИМ ИЗ ФУНКЦИИ
                except Exception as e:
                    logger.error(f"Error sending audio: {e}", exc_info=True)
                    await context.bot.send_message(chat_id, "❌ Ошибка при отправке файла.")
                    return
        
        # Если ни один из 5 треков не удалось скачать
        await msg.edit_text("😕 Не удалось скачать трек: Все найденные варианты заблокированы или весят больше 20 МБ.")
    else:
        await msg.edit_text("😕 Ничего не найдено по этому запросу.")


async def _do_radio(chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, update: Update):
    effective_query = query or "случайные популярные треки"
    await context.bot.send_message(chat_id, f"🎧 Включаю радио-волну: *{effective_query}*", parse_mode=ParseMode.MARKDOWN)
    radio_manager = context.application.radio_manager
    asyncio.create_task(radio_manager.start(chat_id, effective_query, chat_type=update.effective_chat.type))


async def _do_chat_reply(chat_id: int, text: str, user_name: str, context: ContextTypes.DEFAULT_TYPE, update: Update):
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    chat_manager = context.application.chat_manager
    response = await chat_manager.get_response(chat_id, text, user_name)
    if response: await context.bot.send_message(chat_id, response)

# --- Handlers ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text: return
    chat_id = update.effective_chat.id
    message_text = message.text

    if "open.spotify.com/track" in message_text:
        match = re.search(r'(https?://open\.spotify\.com/track/[a-zA-Z0-9]+)', message_text)
        if match: await _do_spotify_play(chat_id, match.group(1), context)
        return

    is_private = update.effective_chat.type == ChatType.PRIVATE
    is_reply = message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id
    is_mention = any(m in message_text.lower() for m in ["аврора", "aurora", "бот", "dj"])

    if is_private or is_reply or is_mention:
        await _do_chat_reply(chat_id, message_text, update.effective_user.first_name, context, update)
        return

    ai_manager = context.application.ai_manager
    analysis = await ai_manager.analyze_message(message_text)
    intent, query = analysis.get("intent"), analysis.get("query")
    
    if intent == 'search' and query: await _do_play(chat_id, query, context)
    elif intent == 'radio' and query: await _do_radio(chat_id, query, context, update)
    elif intent == 'chat': await _do_chat_reply(chat_id, message_text, update.effective_user.first_name, context, update)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎧 Aurora AI DJ. Включаю радио или ищу треки. С чего начнем?")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Что найти? /play <запрос>")
        return
    await _do_play(update.effective_chat.id, " ".join(context.args), context)

async def radio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_radio(update.effective_chat.id, " ".join(context.args), context, update)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    radio_manager = context.application.radio_manager
    if await radio_manager.stop(update.effective_chat.id):
        await context.bot.send_message(update.effective_chat.id, "🛑 Радио остановлено.")

async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    radio_manager = context.application.radio_manager
    await radio_manager.skip(update.effective_chat.id)
    await context.bot.send_message(update.effective_chat.id, "⏭ Переключаю трек...", disable_notification=True)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = context.application.settings

    is_admin = (user_id in settings.ADMIN_ID_LIST) or (str(user_id) in str(settings.ADMIN_IDS))

    if not is_admin:
        await update.message.reply_text(f"⛔️ Вы не админ.\nВаш ID: `{user_id}`\nВставьте его в ADMIN_IDS в Railway.", parse_mode=ParseMode.MARKDOWN)
        return

    current_mode = context.application.chat_manager.get_mode(update.effective_chat.id)
    mode_names = {
        "default": "Эстет",
        "standup": "Комик",
        "expert": "Эксперт",
        "gop": "Гопник",
        "toxic": "Токсик",
        "chill": "Чилл",
        "cyberpunk": "Хакер 🌐",
        "anime": "Аниме 🌸",
        # 🔥 НОВЫЕ КНОПКИ ДЛЯ АДМИНКИ
        "joker": "Анекдоты 🤡",
        "news": "Новости 📰"
    }
    
    keyboard = [[InlineKeyboardButton(f"{'✅ ' if mode == current_mode else ''}{mode_names.get(mode, mode)}", callback_data=f"set_mode|{mode}")] for mode in PERSONAS.keys()]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="close_admin")])
    await context.bot.send_message(update.effective_chat.id, f"🤖 Режим AI: *{current_mode.upper()}*", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    settings = context.application.settings
    chat_id = update.effective_chat.id
    
    if query.data == "close_admin":
        await query.delete_message()
        return

    if query.data == "skip_track":
        await context.application.radio_manager.skip(chat_id)
        try:
            player_url = getattr(settings, 'PLAYER_URL', '') or getattr(settings, 'BASE_URL', '') or getattr(settings, 'WEBHOOK_URL', '').replace('/telegram', '')
            if player_url:
                if not player_url.startswith('http'): player_url = f"https://{player_url}"
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Плеер", url=player_url)]]))
            else:
                await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return

    if query.data.startswith("set_mode|"):
        is_admin = (user_id in settings.ADMIN_ID_LIST) or (str(user_id) in str(settings.ADMIN_IDS))
        if not is_admin:
            await query.answer("⛔️ Только для админа!", show_alert=True)
            return
            
        mode = query.data.split("|")[1]
        context.application.chat_manager.set_mode(chat_id, mode)
        
        greeting = random.choice(GREETINGS.get(mode, ["Привет!"]))
        await context.bot.send_message(chat_id, greeting)
        await query.delete_message()

# 🔥 ИДЕЯ 4: ГОЛОСОВОЕ УПРАВЛЕНИЕ (Gemma 3 Edition)
async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await context.bot.send_message(chat_id, "🎧 <i>Анализирую голос...</i>", parse_mode=ParseMode.HTML)

    try:
        voice_file = await update.message.voice.get_file()
        voice_bytes = await voice_file.download_as_bytearray()

        ai_manager = context.application.ai_manager
        
        # Вызываем Gemma 3 для расшифровки
        transcribed_text = await ai_manager.transcribe_voice(voice_bytes)
        
        if not transcribed_text:
            await msg.edit_text("❌ ИИ не смог разобрать слова. Повторите четче.")
            return

        await msg.edit_text(f"🗣 <b>Вы сказали:</b> {transcribed_text}", parse_mode=ParseMode.HTML)

        # Передаем расшифрованный текст в анализатор
        analysis = await ai_manager.analyze_message(transcribed_text)
        intent, query = analysis.get("intent"), analysis.get("query")
        
        user_name = update.effective_user.first_name
        
        # NOTE: The user-provided code for handling '|' dedications was inconsistent
        # with the _do_play function signature, so it has been simplified to pass the whole query.
        if intent == 'search' and query:
            await _do_play(chat_id, query, context)
        elif intent == 'radio' and query: 
            await _do_radio(chat_id, query, context, update)
        elif intent == 'chat': 
            await _do_chat_reply(chat_id, transcribed_text, user_name, context, update)

    except Exception as e:
        logger.error(f"Voice error: {e}", exc_info=True)
        await msg.edit_text("❌ Ошибка распознавания голоса. Попробуйте еще раз.")

def setup_handlers(app: Application):
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("play", play_command))
    app.add_handler(CommandHandler("radio", radio_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
