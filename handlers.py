from __future__ import annotations
import logging
import asyncio
import json
import random
import re
import os
import time # ⚠️ ВОТ ЭТОТ ИМПОРТ МЫ ДОБАВИЛИ ДЛЯ ВИКТОРИНЫ
from difflib import SequenceMatcher # ⚠️ ДЛЯ ПРОЩЕНИЯ ОПЕЧАТОК!

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
    "joker": ["Слышали анекдот про басиста? Потом расскажу! 🎉", "Время шуток и хорошей музыки! 😂"],
    "news": ["В эфире экстренный выпуск новостей музыки. 📰", "Сводка новостей: вы подключились. 📡"]
}

# 🔥 АЛГОРИТМ "ПРОЩЕНИЯ ОПЕЧАТОК" (Fuzzy Matching)
def is_fuzzy_match(user_input: str, target: str) -> bool:
    if not user_input or not target: return False
    user_input = user_input.lower()
    target = target.lower()
    
    # Убираем все скобки (Remix, feat) из оригинала
    target_clean = re.sub(r'\(.*?\)|\[.*?\]', '', target)
    words = target_clean.split()
    words.append(target_clean.replace(" ", "")) # Целая строка без пробелов
    
    user_clean = user_input.replace(" ", "")
    
    if len(user_clean) < 4:
        return user_clean in words
        
    for w in words:
        w_clean = ''.join(c for c in w if c.isalnum())
        if not w_clean: continue
        # Если юзер ввел часть слова
        if user_clean in w_clean: return True
        # Если опечатка (например "моргин" совпадает с "морген" на 85%)
        if SequenceMatcher(None, user_clean, w_clean).ratio() >= 0.75:
            return True
    return False

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
                            reply_markup=markup
                        )
                    return 
                except Exception as e:
                    logger.error(f"Error sending audio: {e}", exc_info=True)
                    await context.bot.send_message(chat_id, "❌ Ошибка при отправке файла.")
                    return
        await msg.edit_text("😕 Не удалось скачать трек: Все найденные варианты заблокированы.")
    else:
        await msg.edit_text("😕 Ничего не найдено по этому запросу.")

async def _do_radio(chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE):
    effective_query = query or "случайные популярные треки"
    await context.bot.send_message(chat_id, f"🎧 Включаю радио-волну: *{effective_query}*", parse_mode=ParseMode.MARKDOWN)
    radio_manager = context.application.radio_manager
    asyncio.create_task(radio_manager.start(chat_id, effective_query))

async def _do_chat_reply(chat_id: int, text: str, user_name: str, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    chat_manager = context.application.chat_manager
    response = await chat_manager.get_response(chat_id, text, user_name)
    if response: await context.bot.send_message(chat_id, response)

# --- Handlers ---

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

        analysis = await ai_manager.analyze_message(transcribed_text)
        intent, query = analysis.get("intent"), analysis.get("query")
        user_name = update.effective_user.first_name
        
        # 🎮 ЕСЛИ ИДЕТ ВИКТОРИНА - ГОЛОС ИДЕТ СЮДА
        session = context.application.radio_manager._sessions.get(chat_id)
        if session and getattr(session, 'quiz_active', False):
            update.effective_message.text = transcribed_text 
            await text_handler(update, context)
            return

        if intent == 'search' and query:
            if "|" in query:
                q, d = query.split("|", 1)
                await _do_play(chat_id, q.strip(), context, dedication=d.strip())
            else: await _do_play(chat_id, query, context)
        elif intent == 'radio' and query: await _do_radio(chat_id, query, context)
        elif intent == 'chat': await _do_chat_reply(chat_id, transcribed_text, user_name, context)

    except Exception as e:
        logger.error(f"Voice error: {e}", exc_info=True)
        await msg.edit_text("❌ Ошибка распознавания голоса. Попробуйте еще раз.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text: return
    chat_id = update.effective_chat.id
    message_text = message.text

    # 🎮 ПРИОРИТЕТ 1: ЕСЛИ ИДЕТ ВИКТОРИНА - ЭТО ОТВЕТ, А НЕ ЗАПРОС ИИ!
    session = context.application.radio_manager._sessions.get(chat_id)
    if session and getattr(session, 'quiz_active', False):
        artist = session.quiz_artist
        title = session.quiz_title

        is_match = False
        if is_fuzzy_match(message_text, artist) or is_fuzzy_match(message_text, title):
            is_match = True

        if is_match:
            session.quiz_active = False # Останавливаем игру
            
            user_id = update.effective_user.id
            winner_name = update.effective_user.first_name
            if 'scores' not in context.chat_data: context.chat_data['scores'] = {}
            context.chat_data['scores'][user_id] = context.chat_data['scores'].get(user_id, 0) + 1
            score = context.chat_data['scores'][user_id]
            
            prompt = f"В нашей викторине пользователь {winner_name} только что первым угадал песню! Это был трек: {session.quiz_full}. Похвали его очень круто в своем стиле и скажи, что у него теперь {score} очков!"
            announcement = await context.application.chat_manager.get_response(chat_id, prompt, "System")
            await context.bot.send_message(chat_id, f"🎉 🎙 {announcement}")
            return
            
        # ⚠️ Если идет игра, но ответ неверный - мы ПРОСТО ИГНОРИРУЕМ ТЕКСТ. 
        # Не отдаем его ИИ, чтобы не включались другие песни!
        return 

    # --- Стандартная обработка, если игры нет ---
    
    if "open.spotify.com/track" in message_text:
        match = re.search(r'(https?://open\.spotify\.com/track/[a-zA-Z0-9]+)', message_text)
        if match: await _do_spotify_play(chat_id, match.group(1), context)
        return

    is_private = update.effective_chat.type == ChatType.PRIVATE
    is_reply = message.reply_to_message and message.reply_to_message.from_user.id == context.bot.id
    is_mention = any(m in message_text.lower() for m in ["аврора", "aurora", "бот", "dj"])

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
    elif intent == 'radio' and query: await _do_radio(chat_id, query, context)
    elif intent == 'chat': await _do_chat_reply(chat_id, message_text, update.effective_user.first_name, context)

# 🔥 КОМАНДА ЗАПУСКА ИГРЫ "УГАДАЙ МЕЛОДИЮ" (Связь с Радио)
async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = context.application.radio_manager._sessions.get(chat_id)
    
    if not session:
        await update.message.reply_text("❌ Сначала запустите радио (/radio), чтобы играть в викторину!")
        return
        
    if getattr(session, 'quiz_active', False):
        await update.message.reply_text("❌ Игра уже идет! Слушайте голосовое сообщение и пишите варианты в чат.")
        return

    session.last_quiz_time = time.time() # Сбрасываем авто-таймер
    asyncio.create_task(session.run_quiz())


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎧 Aurora AI DJ. Включаю радио или ищу треки. С чего начнем?")

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Что найти? Введите:\n`/play песня | ваше послание`", parse_mode=ParseMode.MARKDOWN)
        return
    raw_query = " ".join(context.args)
    if "|" in raw_query:
        q, d = raw_query.split("|", 1)
        await _do_play(update.effective_chat.id, q.strip(), context, dedication=d.strip())
    else:
        await _do_play(update.effective_chat.id, raw_query, context)

async def radio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_radio(update.effective_chat.id, " ".join(context.args), context)

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
    mode_names = { "default": "Эстет", "standup": "Комик", "expert": "Эксперт", "gop": "Гопник", "toxic": "Токсик", "chill": "Чилл", "cyberpunk": "Хакер 🌐", "anime": "Аниме 🌸", "joker": "Анекдоты 🤡", "news": "Новости 📰" }
    
    keyboard = [[InlineKeyboardButton(f"{'✅ ' if mode == current_mode else ''}{mode_names.get(mode, mode)}", callback_data=f"set_mode|{mode}")] for mode in PERSONAS.keys()]
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
        await context.application.radio_manager.skip(update.effective_chat.id)
        try:
            player_url = getattr(settings, 'PLAYER_URL', '') or getattr(settings, 'BASE_URL', '') or getattr(settings, 'WEBHOOK_URL', '').replace('/telegram', '')
            if player_url:
                if not player_url.startswith('http'): player_url = f"https://{player_url}"
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Плеер", url=player_url)]]))
            else: await query.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return

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
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(CallbackQueryHandler(button_callback))
