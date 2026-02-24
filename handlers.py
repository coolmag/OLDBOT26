from __future__ import annotations
import logging
import asyncio
import json
import random
import re

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
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
    "standup": ["О, новые зрители! Готовьтесь к прожарке.", "Проверка микрофона... раз-два."]
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
    tracks = await downloader.search(query, limit=1)

    if tracks:
        dl_res = await downloader.download(tracks[0].identifier, tracks[0])

        if dl_res.success and dl_res.file_path:
            await msg.delete() # Удаляем только если скачали успешно
            try:
                info = dl_res.track_info
                
                # 🎛 ЖЕЛЕЗОБЕТОННАЯ КНОПКА ПЛЕЕРА
                settings = context.application.settings
                # Пытаемся взять PLAYER_URL, если нет - BASE_URL, если нет - вырезаем из WEBHOOK_URL
                player_url = getattr(settings, 'PLAYER_URL', '') or getattr(settings, 'BASE_URL', '') or getattr(settings, 'WEBHOOK_URL', '').replace('/telegram', '')
                
                markup = None
                if player_url:
                    if not player_url.startswith('http'): player_url = f"https://{player_url}"
                    markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Плеер", web_app=WebAppInfo(url=player_url))]])
                
                logger.info(f"Отправка файла {info.title} с плеером: {player_url}")
                with open(dl_res.file_path, 'rb') as f:
                    await context.bot.send_audio(
                        chat_id=chat_id, audio=f,
                        title=info.title if info else "Track", 
                        performer=info.artist if info else "Unknown", 
                        duration=info.duration if info else 0,
                        reply_markup=markup # Прикрепляем кнопку
                    )
            except Exception as e:
                logger.error(f"Error sending audio: {e}", exc_info=True)
                await context.bot.send_message(chat_id, "❌ Ошибка при отправке файла.")
        else:
             await msg.edit_text(f"😕 Не удалось скачать трек: {dl_res.error_message}")
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
    mode_names = {"default": "Эстет", "standup": "Комик", "expert": "Эксперт", "gop": "Гопник", "toxic": "Токсик", "chill": "Чилл"}
    
    keyboard = [[InlineKeyboardButton(f"{'✅ ' if mode == current_mode else ''}{mode_names.get(mode, mode)}", callback_data=f"set_mode|{mode}")] for mode in PERSONAS.keys()]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="close_admin")])
    await update.message.reply_text(f"🤖 Режим AI: *{current_mode.upper()}*", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    settings = context.application.settings
    
    if query.data == "close_admin":
        await query.delete_message()
        return

    if query.data == "skip_track":
        await context.application.radio_manager.skip(update.effective_chat.id)
        try:
            # Убираем кнопку скипа, оставляем только плеер
            player_url = getattr(settings, 'PLAYER_URL', '') or getattr(settings, 'BASE_URL', '') or getattr(settings, 'WEBHOOK_URL', '').replace('/telegram', '')
            if player_url:
                if not player_url.startswith('http'): player_url = f"https://{player_url}"
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Плеер", web_app=WebAppInfo(url=player_url))]]))
            else:
                await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return

    # ⚠️ ВОТ ИСПРАВЛЕНИЕ: Теперь бот правильно проверяет права при нажатии на кнопку!
    if query.data.startswith("set_mode|"):
        is_admin = (user_id in settings.ADMIN_ID_LIST) or (str(user_id) in str(settings.ADMIN_IDS))
        if not is_admin:
            await query.answer("⛔️ Только для админа!", show_alert=True)
            return
            
        mode = query.data.split("|")[1]
        context.application.chat_manager.set_mode(update.effective_chat.id, mode)
        
        greeting = random.choice(GREETINGS.get(mode, ["Привет!"]))
        await context.bot.send_message(update.effective_chat.id, greeting)
        await query.delete_message()

def setup_handlers(app: Application):
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("play", play_command))
    app.add_handler(CommandHandler("radio", radio_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
