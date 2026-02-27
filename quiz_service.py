import asyncio
import logging
import os
import random
import re
from pathlib import Path
from difflib import SequenceMatcher
from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger("quiz_service")

def is_fuzzy_match(user_input: str, target: str) -> bool:
    if not user_input or not target: return False
    u = user_input.lower().strip()
    t = target.lower()
    
    t_clean = re.sub(r'\(.*?\)|\[.*?\]', '', t).strip()
    if not t_clean: t_clean = t
    
    # Прямое вхождение строки целиком
    if u in t_clean or t_clean in u: return True
    
    words = t_clean.split()
    for w in words:
        w_c = ''.join(c for c in w if c.isalnum())
        if len(w_c) >= 3:
            # Вхождение отдельного слова (например, "асти" в "anna asti")
            if u in w_c or w_c in u: return True
            # Опечатки (например "моргин" вместо "морген")
            if SequenceMatcher(None, u, w_c).ratio() > 0.75: return True
            
    return False

class QuizManager:
    def __init__(self, settings, downloader, chat_manager):
        self.settings = settings
        self.downloader = downloader
        self.chat_manager = chat_manager
        self.sessions = {} # Хранилище статусов игр по чатам
        self.scores = {}

    def is_active(self, chat_id: int) -> bool:
        return self.sessions.get(chat_id, {}).get('active', False)

    async def process_answer(self, chat_id: int, user_id: int, user_name: str, text: str, bot: Bot) -> bool:
        session = self.sessions.get(chat_id)
        if not session or not session['active']: return False
        
        # Проверяем ответ
        is_match = is_fuzzy_match(text, session['artist']) or is_fuzzy_match(text, session['title'])
        
        if is_match:
            session['active'] = False
            session['event'].set() # Мгновенно останавливаем 30-секундный таймер
            
            self.scores[user_id] = self.scores.get(user_id, 0) + 1
            
            prompt = f"В викторине юзер {user_name} первым угадал песню! Это: {session['full']}. Похвали его очень круто в своем стиле и скажи, что у него теперь {self.scores[user_id]} очков!"
            announcement = await self.chat_manager.get_response(chat_id, prompt, "System")
            await bot.send_message(chat_id, f"🎉 🎙 {announcement}")
            return True
            
        return False # Ответ неверный

    async def start_quiz(self, chat_id: int, bot: Bot, radio_manager):
        if self.is_active(chat_id):
            await bot.send_message(chat_id, "❌ Игра уже идет! Слушайте сообщение и пишите варианты в чат.")
            return

        radio_session = radio_manager._sessions.get(chat_id)
        if radio_session: radio_session.quiz_active = True
        
        self.sessions[chat_id] = {'active': True, 'event': asyncio.Event()}
        msg = await bot.send_message(chat_id, "🎲 <i>Настраиваю видео-камеры для викторины...</i>", parse_mode=ParseMode.HTML)

        queries = ["хиты 2000х", "руки вверх", "король и шут", "linkin park", "eminem", "macan", "miyagi", "баста", "anna asti", "zivert", "скриптонит", "t.a.t.u.", "моргенштерн"]
        tracks = await self.downloader.search(random.choice(queries), limit=5)

        if not tracks:
            await msg.edit_text("❌ Ошибка поиска треков.")
            self._cleanup(chat_id, radio_session)
            return

        track = random.choice(tracks[:3])
        dl_res = await self.downloader.download(track.identifier, track)

        if not dl_res or not dl_res.success or not dl_res.file_path:
            await msg.edit_text("❌ Ошибка загрузки трека.")
            self._cleanup(chat_id, radio_session)
            return

        info = dl_res.track_info
        input_audio = str(dl_res.file_path)
        
        # ⚠️ ПУТЬ К ТВОЕМУ ВИДЕО-АВАТАРУ
        input_video = str(self.settings.BASE_DIR / "avatar.mp4")
        
        output_video = str(self.settings.DOWNLOADS_DIR / f"quiz_{track.identifier}.mp4")
        start_time = max(0, (info.duration // 2) - 10) if info.duration else 30

        try:
            # 🔥 КИНЕМАТОГРАФИЧЕСКАЯ СКЛЕЙКА (FFMPEG)
            # Если видео-аватара нет, делаем обычную голосовуху. Если есть - делаем ВИДЕО-КРУЖОК!
            if os.path.exists(input_video):
                # Берем видео (-stream_loop 1 зацикливает короткое видео), накладываем на него звук, обрезаем ровно до 15 секунд
                cmd = [
                    'ffmpeg', '-y', 
                    '-stream_loop', '-1', '-i', input_video,  # Бесконечный луп видео
                    '-ss', str(start_time), '-i', input_audio, # Звук с нужной секунды
                    '-t', '15', # Длина ровно 15 сек
                    '-map', '0:v:0', '-map', '1:a:0', # Склеиваем видео с первой дорожки и звук со второй
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', # Быстрое кодирование H.264
                    '-vf', 'scale=480:480,crop=480:480', # Жесткий квадрат 480x480 для кружочка
                    '-c:a', 'aac', '-b:a', '128k', # Аудио в AAC (стандарт ТГ)
                    '-shortest', # Обрезать по самому короткому потоку
                    output_video
                ]
            else:
                logger.warning("avatar.mp4 не найден! Падаю на обычную голосовуху.")
                output_video = str(self.settings.DOWNLOADS_DIR / f"quiz_{track.identifier}.ogg")
                cmd = [
                    'ffmpeg', '-y', '-i', input_audio, 
                    '-ss', str(start_time), '-t', '15', 
                    '-c:a', 'libopus', '-b:a', '32k', 
                    '-ac', '1', '-ar', '48000', 
                    output_video
                ]

            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()

            if not os.path.exists(output_video) or os.path.getsize(output_video) == 0:
                raise Exception("FFmpeg failed to create media")

            await msg.delete()

            prompt = "Ты ведешь игру 'Угадай мелодию'. Энергично скажи: 'Смотрим в эфир! 15 секунд музыки. Кто первый назовет трек или артиста — заберет очки!'"
            announcement = await self.chat_manager.get_response(chat_id, prompt, "System")
            if announcement: 
                await bot.send_message(chat_id, f"🎙 {announcement}")

            # ⚠️ ОТПРАВКА: Если сделали MP4 - шлем как Video Note (кружок), иначе Voice
            with open(output_video, 'rb') as f:
                if output_video.endswith('.mp4'):
                    await bot.send_video_note(chat_id, video_note=f, length=480)
                else:
                    await bot.send_voice(chat_id, voice=f.read(), filename="quiz.ogg")

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
            await bot.send_message(chat_id, "❌ Сбой аппаратуры. Проверьте логи.")
        finally:
            self._cleanup(chat_id, radio_session)
            if getattr(dl_res, 'is_url', False) == False and os.path.exists(input_audio): 
                try: os.unlink(input_audio)
                except: pass
            if os.path.exists(output_video): 
                try: os.unlink(output_video)
                except: pass

    def _cleanup(self, chat_id, radio_session):
        # ⚠️ ОБЯЗАТЕЛЬНАЯ СЕКЦИЯ: СНИМАЕМ ИГРУ И РАДИО С ПАУЗЫ ПРИ ЛЮБОМ ИСХОДЕ
        if chat_id in self.sessions:
            self.sessions[chat_id]['active'] = False
        if radio_session:
            radio_session.quiz_active = False