import asyncio
import logging
import os
import random
import re
import json
from pathlib import Path
from difflib import SequenceMatcher
from typing import Optional
from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger("quiz_service")

# Load music catalog
try:
    with open(Path(__file__).parent / "genres.json", "r", encoding="utf-8") as f:
        MUSIC_CATALOG = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    MUSIC_CATALOG = {}
    logger.error("Could not load genres.json for quiz service!")

def get_random_catalog_query() -> tuple[str, Optional[str], str]:
    """Selects a random query from the music catalog."""
    all_queries = []
    def extract(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, dict):
                    if "query" in v: all_queries.append((v["query"], v.get("decade"), v.get("name", k)))
                    elif "children" in v: extract(v["children"])
                elif isinstance(v, list): extract(v)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, dict) and "query" in item: all_queries.append((item["query"], item.get("decade"), item.get("name", "Unknown")))
                elif isinstance(item, dict): extract(item)
    extract(MUSIC_CATALOG)
    return random.choice(all_queries) if all_queries else ("top hits", None, "Random")


# 🔥 ПЛАН Б: Резервный локальный алгоритм на случай смерти ИИ-судьи
def is_fuzzy_match(user_input: str, target: str) -> bool:
    if not user_input or not target: return False
    u = user_input.lower().strip()
    t = target.lower()

    t_clean = re.sub(r'\(.*?\)|\[.*?\]', '', t).strip()
    if not t_clean: t_clean = t

    if u in t_clean or t_clean in u: return True

    words = t_clean.split()
    for w in words:
        w_c = ''.join(c for c in w if c.isalnum())
        if len(w_c) >= 3:
            if u in w_c or w_c in u: return True
            if SequenceMatcher(None, u, w_c).ratio() > 0.75: return True
    return False


class QuizManager:
    def __init__(self, settings, downloader, chat_manager, cache):
        self.settings = settings
        self.downloader = downloader
        self.chat_manager = chat_manager
        self.cache = cache  # База данных для вечного хранения очков
        self.sessions = {}
        self.scores = {}

    def is_active(self, chat_id: int) -> bool:
        return self.sessions.get(chat_id, {}).get('active', False)

    async def process_answer(self, chat_id: int, user_id: int, user_name: str, text: str, bot: Bot) -> bool:
        session = self.sessions.get(chat_id)

        # 🟢 ЗАЩИТА (Гонка потоков): Если юзер пишет в чат, пока видео еще рендерится (ответа еще нет в сессии)
        if not session or not session.get('active') or 'full' not in session:
            return False

        is_match = False

        # 🛡️ ПЛАН А: Умный ИИ-Судья (На модели Gemma 3)
        try:
            prompt = f"""
            Сейчас идет викторина "Угадай мелодию". Правильный ответ: {session['full']}.
            Пользователь написал: "{text}"
            Твоя задача — строго решить, угадал ли он.
            Если ответ ПРАВИЛЬНЫЙ (даже с опечатками или на другом языке, "Мияги"="Miyagi"), напиши ровно одно слово: ДА.
            Если ответ НЕВЕРНЫЙ, напиши ровно одно слово: НЕТ.
            """
            # Правильный асинхронный вызов Google GenAI API
            ai_verdict = await asyncio.wait_for(
                self.chat_manager.ai_manager.gemini_client.aio.models.generate_content(
                    model="gemma-3-27b-it",
                    contents=prompt
                ),
                timeout=5.0
            )
            verdict_text = ai_verdict.text.strip().upper()
            if "ДА" in verdict_text:
                is_match = True

        except Exception as e:
            # 🛡️ ПЛАН Б: ИИ упал. Включаем резервный локальный алгоритм!
            logger.warning(f"⚠️ ИИ-Судья недоступен ({e}). Переход на локальный Fuzzy Match.")
            if is_fuzzy_match(text, session['artist']) or is_fuzzy_match(text, session['title']):
                is_match = True

        # Если юзер угадал
        if is_match:
            session['active'] = False
            session['event'].set()

            # Сохраняем очки в вечную БД, чтобы они не пропадали при перезагрузке бота
            current_score = await self.cache.get(f"score_{user_id}") or 0
            new_score = current_score + 1
            await self.cache.set(f"score_{user_id}", new_score, ttl=0)
            self.scores[user_id] = new_score

            praise_prompt = f"В викторине юзер {user_name} только что первым угадал песню ({text})! Это был трек: {session['full']}. Похвали его очень круто в своем стиле и скажи, что у него теперь {new_score} очков!"
            announcement = await self.chat_manager.get_response(chat_id, praise_prompt, "System")
            await bot.send_message(chat_id, f"🎉 🎙 {announcement}")
            return True

        return False

    async def start_quiz(self, chat_id: int, bot: Bot):
        if self.is_active(chat_id):
            try:
                await bot.send_message(chat_id, "❌ Игра уже идет! Слушайте видео-сообщение и пишите варианты в чат.")
            except Exception: pass
            return

        self.sessions[chat_id] = {'active': True, 'event': asyncio.Event()}

        # 🟢 ДОБАВЛЕНА ЗАЩИТА ОТ ТАЙМАУТОВ TELEGRAM (Сетевых сбоев)
        try:
            msg = await bot.send_message(chat_id, "🎲 <i>Настраиваю видео-камеры для викторины...</i>", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Quiz Network Timeout: Failed to send initial message ({e}). Aborting quiz.")
            self._cleanup(chat_id)
            return # Отменяем викторину, если нет связи с Telegram

        # 🔥🔥🔥 ИЗМЕНЕНИЕ: Используем наш новый каталог вместо старого списка!
        query, _, display_name = get_random_catalog_query()
        logger.info(f"Starting quiz with random genre: '{display_name}' (query: {query})")
        tracks = await self.downloader.search(query, limit=10) # Ищем побольше треков

        if not tracks:
            try: await msg.edit_text(f"❌ Ошибка поиска треков для жанра '{display_name}'.")
            except Exception: pass
            self._cleanup(chat_id)
            return

        track = random.choice(tracks) # Выбираем случайный из найденных
        dl_res = await self.downloader.download(track.identifier, track)

        if not dl_res or not dl_res.success or not dl_res.file_path:
            await msg.edit_text("❌ Ошибка загрузки трека.")
            self._cleanup(chat_id)
            return

        info = dl_res.track_info
        input_audio = str(dl_res.file_path)
        input_video = str(self.settings.BASE_DIR / "avatar.mp4")
        output_video = str(self.settings.DOWNLOADS_DIR / f"quiz_{track.identifier}.mp4")
        start_time = max(0, (info.duration // 2) - 10) if info.duration else 30

        try:
            if os.path.exists(input_video):
                cmd = [
                    'ffmpeg', '-y',
                    '-stream_loop', '-1', '-i', input_video,
                    '-ss', str(start_time), '-i', input_audio,
                    '-t', '15',
                    '-map', '0:v:0', '-map', '1:a:0',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
                    '-vf', 'scale=480:480,crop=480:480',
                    '-c:a', 'aac', '-b:a', '128k',
                    '-shortest',
                    output_video
                ]
            else:
                output_video = str(self.settings.DOWNLOADS_DIR / f"quiz_{track.identifier}.ogg")
                cmd = ['ffmpeg', '-y', '-i', input_audio, '-ss', str(start_time), '-t', '15', '-c:a', 'libopus', '-b:a', '32k', '-ac', '1', '-ar', '48000', output_video]

            # Правильный перехват потоков FFmpeg
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode('utf-8', errors='ignore')
                logger.error(f"FFmpeg failed with code {proc.returncode}. Details: {error_msg}")
                raise Exception("FFmpeg rendering failed")

            if not os.path.exists(output_video) or os.path.getsize(output_video) == 0:
                raise Exception("FFmpeg output file is missing or empty")

            await msg.delete()

            prompt = "Ты ведешь игру 'Угадай мелодию'. Энергично скажи: 'Смотрим в эфир! 15 секунд музыки. Кто первый назовет трек или артиста — заберет очки! Время пошло!'"
            announcement = await self.chat_manager.get_response(chat_id, prompt, "System")
            if announcement:
                await bot.send_message(chat_id, f"🎙 {announcement}")

            with open(output_video, 'rb') as f:
                if output_video.endswith('.mp4'): await bot.send_video_note(chat_id, video_note=f, length=480)
                else: await bot.send_voice(chat_id, voice=f.read(), filename="quiz.ogg")

            # 🟢 В этот момент записываем правильный ответ в память (До этого момента никто не мог его угадать и сломать бота)
            self.sessions[chat_id].update({
                'artist': info.artist,
                'title': info.title,
                'full': f"{info.artist} - {info.title}"
            })

            try:
                await asyncio.wait_for(self.sessions[chat_id]['event'].wait(), timeout=30.0)
            except asyncio.TimeoutError:
                if self.is_active(chat_id):
                    self.sessions[chat_id]['active'] = False
                    prompt = f"Время вышло, никто не угадал! Это был трек: {info.artist} - {info.title}. Высмей их."
                    roast = await self.chat_manager.get_response(chat_id, prompt, "System")
                    await bot.send_message(chat_id, f"⏰ 🎙 {roast}", parse_mode=ParseMode.MARKDOWN)

        except Exception as e:
            logger.error(f"Quiz run error: {e}")
            await bot.send_message(chat_id, "❌ Сбой аппаратуры.")
        finally:
            self._cleanup(chat_id)
            # Удаление файлов отключено: за это теперь отвечает Garbage Collector в main.py

    def _cleanup(self, chat_id):
        if chat_id in self.sessions:
            self.sessions[chat_id]['active'] = False