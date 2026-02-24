from __future__ import annotations
import logging
import asyncio
import json
import random
import re
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters
)

from ai_personas import PERSONAS

logger = logging.getLogger("handlers")

GREETINGS = {
    "default": ["Привет! Я снова я. 🎧", "Режим по умолчанию. Погнали!", "Снова в эфире!"],
    "toxic": ["Ну че, переключил? Теперь терпи.", "Ой, опять ты... Ладно, слушаю.", "Режим токсика активирован. 🙄"],
    "gop": ["Здарова, бродяга! Че каво?", "Ну че, посидим?", "Вечер в хату."],
    "chill": ["Вайб включен... 🌌", "Расслабься...", "Тишина и музыка..."],
    "quiz": ["Время викторины! 🎯", "Я готова задавать вопросы!"]
}

# --- Internal Action Functions ---

async def _do_spotify_play(chat_id: int, spotify_url: str, context: ContextTypes.DEFAULT_TYPE):
    msg = await context.bot.send_message(chat_id, "🎶 Распознал ссылку Spotify. Ищу трек...", disable_notification=True)
    spotify_service = context.application.spotify_service
    dl_res = await spotify_service.download_from_url(spotify_url)
    await msg.delete()
    if dl_res.success and dl_res.file_path:
        try:
            info = dl_res.track_info
            with open(dl_res.file_path, 'rb') as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f, title=info.title, performer=info.artist, duration=info.duration, thumbnail=info.thumbnail_url)
        except Exception as e:
            logger.error(f"Error sending Spotify audio: {e}", exc_info=True)
    else:
        await context.bot.send_message(chat_id, f"😕 Не удалось скачать трек из Spotify: {dl_res.error_message or 'Неизвестная ошибка'}")


async def _do_play(chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE):
    msg = await context.bot.send_message(chat_id, f"🔎 Ищу: *{query[:100]}*...", parse_mode=ParseMode.MARKDOWN, disable_notification=True)
    downloader = context.application.downloader
    tracks = await downloader.search(query, limit=1)
    if tracks:
        await msg.delete()
        dl_res = await downloader.download(tracks[0].identifier, tracks[0])
        if dl_res.success and dl_res.file_path:
            try:
                info = dl_res.track_info
                with open(dl_res.file_path, 'rb') as f:
                    await context.bot.send_audio(chat_id=chat_id, audio=f, title=info.title, performer=info.artist, duration=info.duration, thumbnail=info.thumbnail_url)
            except Exception as e:
                logger.error(f"Error sending audio: {e}", exc_info=True)
        else:
             await context.bot.send_message(chat_id, f"😕 Не удалось скачать трек: {dl_res.error_message or 'Неизвестная ошибка'}")
    else:
        await msg.edit_text("😕 Ничего не найдено по этому запросу.")


async def _do_radio(chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, update: Update):
    effective_query = query or "случайные популярные треки"
    await context.bot.send_message(chat_id, f"🎧 Включаю радио-волну: *{effective_query}*", parse_mode=ParseMode.MARKDOWN)
    radio_manager = context.application.radio_manager
    asyncio.create_task(radio_manager.start(chat_id, effective_query, chat_type=update.effective_chat.type))


async def _do_chat_reply(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE, update: Update):
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    chat_manager = context.application.chat_manager
    response = await chat_manager.get_response(chat_id, text, update.effective_user.first_name)
    try:
        if response.strip().startswith('{') and '"command"' in response:
            data = json.loads(response)
            command, query = data.get("command"), data.get("query")
            if command == "radio": await _do_radio(chat_id, query, context, update)
            elif command == "search": await _do_play(chat_id, query, context)
            return
    except: pass
    if response: await context.bot.send_message(chat_id, response)

# --- Main Text Handler ---

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, chat_id = update.effective_message, update.effective_chat.id
    if not message or not message.text: return
    
    spotify_match = re.search(r'(https?://open\.spotify\.com/track/[a-zA-Z0-9]+)', message.text)
    if spotify_match:
        return await _do_spotify_play(chat_id, spotify_match.group(1), context)

    is_private = update.effective_chat.type == ChatType.PRIVATE
    is_reply = message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id
    is_mention = any(mention in message.text.lower() for mention in ["аврора", "aurora", "бот", "dj"])

    if is_private or is_reply or is_mention:
        return await _do_chat_reply(chat_id, message.text, context, update)

    ai_manager = context.application.ai_manager
    analysis = await ai_manager.analyze_message(message.text)
    intent, query = analysis.get("intent"), analysis.get("query")
    
    if intent == 'search' and query: await _do_play(chat_id, query, context)
    elif intent == 'radio' and query: await _do_radio(chat_id, query, context, update)
    elif intent == 'chat': await _do_chat_reply(chat_id, message.text, context, update)

# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = context.application.settings
    player_url = getattr(settings, 'PLAYER_URL', getattr(settings, 'BASE_URL', ''))
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Плеер", web_app=WebAppInfo(url=player_url))]]) if player_url else None
    await update.message.reply_text("🎧 Aurora AI DJ. Включаю радио или ищу треки. С чего начнем?", reply_markup=reply_markup)

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Что найти? /play <запрос>")
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
    if user_id not in settings.ADMIN_ID_LIST: return
    
    chat_id = update.effective_chat.id
    chat_manager = context.application.chat_manager
    current_mode = chat_manager.get_mode(chat_id)
    
    text = f"🤖 Режим AI: *{current_mode.upper()}*\nВыберите личность:"
    keyboard = [[InlineKeyboardButton(f"{'✅ ' if mode == current_mode else ''}{p['name']}", callback_data=f"set_mode|{mode}")] for mode, p in PERSONAS.items()]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="close_admin")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    settings = context.application.settings
    
    if query.data == "close_admin":
        await query.delete_message()
        return

    # ОБРАБОТКА СКИПА ИЗ ПОД ТРЕКА
    if query.data == "skip_track":
        radio_manager = context.application.radio_manager
        await radio_manager.skip(update.effective_chat.id)
        try:
            # Убираем только кнопку скип, оставляем плеер
            player_url = getattr(settings, 'PLAYER_URL', getattr(settings, 'BASE_URL', ''))
            if player_url:
                new_markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Плеер", web_app=WebAppInfo(url=player_url))]])
                await query.edit_message_reply_markup(reply_markup=new_markup)
            else:
                await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return

    if query.data.startswith("set_mode|"):
        if user_id not in settings.ADMIN_ID_LIST:
            await query.answer("⛔️ Только для админа!", show_alert=True)
            return
            
        mode = query.data.split("|")[1]
        chat_manager = context.application.chat_manager
        chat_manager.set_mode(update.effective_chat.id, mode)
        
        greeting = random.choice(GREETINGS.get(mode, ["Привет!"]))
        await context.bot.send_message(update.effective_chat.id, greeting)
        await query.delete_message()

def setup_handlers(app: Application):
    """Registers all handlers with the application."""
    command_handlers = [
        CommandHandler("start", start_command),
        CommandHandler("play", play_command),
        CommandHandler("radio", radio_command),
        CommandHandler("stop", stop_command),
        CommandHandler("skip", skip_command),
        CommandHandler("admin", admin_command)
    ]
    app.add_handlers(command_handlers)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
