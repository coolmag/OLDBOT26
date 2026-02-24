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

# Services are now accessed via context.application.*
# No direct imports of RadioManager, YouTubeDownloader, etc.

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
    """Handles downloading and sending a track from a Spotify URL."""
    msg = await context.bot.send_message(
        chat_id, "🎶 Распознал ссылку Spotify. Ищу трек...",
        disable_notification=True
    )
    
    spotify_service = context.application.spotify_service
    dl_res = await spotify_service.download_from_url(spotify_url)

    await msg.delete()

    if dl_res.success and dl_res.file_path:
        try:
            info = dl_res.track_info
            with open(dl_res.file_path, 'rb') as f:
                await context.bot.send_audio(
                    chat_id=chat_id, audio=f,
                    title=info.title if info else "Track",
                    performer=info.artist if info else "Unknown",
                    duration=info.duration if info else 0,
                    thumbnail=info.thumbnail_url if info else None
                )
        except Exception as e:
            logger.error(f"Error sending Spotify audio: {e}", exc_info=True)
            await context.bot.send_message(chat_id, "❌ Ошибка при отправке файла.")
    else:
        await context.bot.send_message(chat_id, f"😕 Не удалось скачать трек из Spotify: {dl_res.error_message or 'Неизвестная ошибка'}")


async def _do_play(chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE):
    msg = await context.bot.send_message(
        chat_id, f"🔎 Ищу: *{query[:100]}*...",
        parse_mode=ParseMode.MARKDOWN, disable_notification=True
    )

    downloader = context.application.downloader
    tracks = await downloader.search(query, limit=1)

    if tracks:
        await msg.delete()
        dl_res = await downloader.download(tracks[0].id, tracks[0])

        if dl_res.success and dl_res.file_path:
            try:
                info = dl_res.track_info
                with open(dl_res.file_path, 'rb') as f:
                    await context.bot.send_audio(
                        chat_id=chat_id, audio=f,
                        title=info.title, performer=info.artist, duration=info.duration,
                        thumbnail=info.thumbnail_url
                    )
            except Exception as e:
                logger.error(f"Error sending audio: {e}", exc_info=True)
                await context.bot.send_message(chat_id, "❌ Ошибка при отправке файла.")
        else:
             await context.bot.send_message(chat_id, f"😕 Не удалось скачать трек: {dl_res.error_message or 'Неизвестная ошибка'}")
    else:
        await msg.edit_text("😕 Ничего не найдено по этому запросу.")


async def _do_radio(chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, update: Update):
    effective_query = query or "случайные популярные треки"
    await context.bot.send_message(chat_id, f"🎧 Включаю радио-волну: *{effective_query}*", parse_mode=ParseMode.MARKDOWN)
    
    radio_manager = context.application.radio_manager
    # Pass the chat_type from the update object
    asyncio.create_task(radio_manager.start(chat_id, effective_query, chat_type=update.effective_chat.type))


async def _do_chat_reply(chat_id: int, text: str, user_name: str, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    
    chat_manager = context.application.chat_manager
    response = await chat_manager.get_response(chat_id, text, user_name)
    
    # Check if AI wants to execute a command
    try:
        # A simple way to check for a JSON command without complex parsing
        if response.strip().startswith('{') and '"command"' in response:
            data = json.loads(response)
            command = data.get("command")
            query = data.get("query")
            
            if command == "radio":
                await _do_radio(chat_id, query, context)
                return
            elif command == "search":
                await _do_play(chat_id, query, context)
                return
    except (json.JSONDecodeError, TypeError):
        pass # Not a valid command, just a regular message

    if response:
        await context.bot.send_message(chat_id, response)


# --- Main Text Handler ---

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text: return
    
    chat_id = update.effective_chat.id
    message_text = message.text

    # 1. Spotify URL check
    spotify_match = re.search(r'(https?://open\.spotify\.com/track/[a-zA-Z0-9]+)', message_text)
    if spotify_match:
        await _do_spotify_play(chat_id, spotify_match.group(1), context)
        return

    # 2. Determine if bot should reply (private chat, reply, or mention)
    is_private = update.effective_chat.type == ChatType.PRIVATE
    is_reply = message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id
    is_mention = any(mention in message_text.lower() for mention in ["аврора", "aurora", "бот", "dj"])

    if is_private or is_reply or is_mention:
        await _do_chat_reply(chat_id, message_text, update.effective_user.first_name, context)
        return

    # 3. If not a direct interaction, analyze intent
    ai_manager = context.application.ai_manager
    analysis = await ai_manager.analyze_message(message_text)
    intent = analysis.get("intent")
    query = analysis.get("query")
    
    logger.info(f"AI Analysis: '{message_text}' -> Intent: {intent}, Query: '{query}'")

    if intent == 'search' and query:
        await _do_play(chat_id, query, context)
    elif intent == 'radio' and query:
        await _do_radio(chat_id, query, context)
    elif intent == 'chat':
        # AI decided it's a chat message even without direct mention
        await _do_chat_reply(chat_id, message_text, update.effective_user.first_name, context)


# --- Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = context.application.settings
    keyboard = [[WebAppInfo(url=settings.PLAYER_URL)]] if settings.PLAYER_URL else None
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть плеер", web_app=WebAppInfo(url=settings.PLAYER_URL))]]) if settings.PLAYER_URL else None
    await update.message.reply_text("🎧 Aurora AI DJ. Включаю радио или ищу треки. С чего начнем?", reply_markup=reply_markup)


async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Что найти? /play <запрос>")
        return
    await _do_play(update.effective_chat.id, " ".join(context.args), context)


async def radio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_radio(update.effective_chat.id, " ".join(context.args), context, update)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    radio_manager = context.application.radio_manager
    was_stopped = await radio_manager.stop(update.effective_chat.id)
    if was_stopped:
        await context.bot.send_message(update.effective_chat.id, "🛑 Радио остановлено.")


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skips the current track in radio mode."""
    radio_manager = context.application.radio_manager
    await radio_manager.skip(update.effective_chat.id)
    # Give a silent notification that the skip is happening
    try:
        await context.bot.send_message(update.effective_chat.id, "⏭ Переключаю трек...", disable_notification=True, reply_to_message_id=update.message.message_id)
    except:
        pass # Ignore if original message is deleted


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = context.application.settings

    if user_id not in settings.ADMIN_ID_LIST:
        return # Silently ignore non-admins

    chat_id = update.effective_chat.id
    chat_manager = context.application.chat_manager
    current_mode = chat_manager.get_mode(chat_id)
    
    text = f"🤖 Режим AI: *{current_mode.upper()}*\nВыберите личность:"
    keyboard = [
        [InlineKeyboardButton(f"{'✅ ' if mode == current_mode else ''}{p['name']}", callback_data=f"set_mode|{mode}")]
        for mode, p in PERSONAS.items()
    ]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="close_admin")])
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))


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
        radio_manager = context.application.radio_manager
        await radio_manager.skip(chat_id)
        # Immediately delete the message with the skip button to prevent multiple skips
        try:
            await query.delete_message()
        except:
            pass # Message might be already gone
        return

    if query.data.startswith("set_mode|"):
        if user_id not in settings.ADMIN_ID_LIST:
            await query.answer("⛔️ Только для админа!", show_alert=True)
            return
            
        mode = query.data.split("|")[1]
        chat_manager = context.application.chat_manager
        chat_manager.set_mode(chat_id, mode)
        
        greeting = random.choice(GREETINGS.get(mode, ["Привет!"]))
        await context.bot.send_message(chat_id, greeting)
        await query.delete_message()


def setup_handlers(app: Application):
    """Registers all handlers with the application."""
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("play", play_command))
    app.add_handler(CommandHandler("radio", radio_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("skip", skip_command))
    app.add_handler(CommandHandler("admin", admin_command))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
