import asyncio
import logging
import random
import os
import time
import json
import edge_tts
import re
from pathlib import Path
from typing import List, Optional, Dict, Set
from dataclasses import dataclass, field

from telegram import Bot, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest, Forbidden

from config import Settings
from models import TrackInfo, DownloadResult
from youtube import YouTubeDownloader
from chat_service import ChatManager

# Загрузка каталога жанров
with open(Path(__file__).parent / "genres.json", "r", encoding="utf-8") as f:
    MUSIC_CATALOG = json.load(f)

# ⚠️ ВОТ ЭТА СТРОКА БЫЛА ПОТЕРЯНА. ОНА КРИТИЧЕСКИ ВАЖНА ДЛЯ ЛОГОВ!
logger = logging.getLogger(__name__)


async def merge_audio(voice_path: str, track_path: str, output_path: str) -> bool:
    """Аппаратная склейка голоса и трека через FFmpeg"""
    cmd = [
        'ffmpeg', '-y',
        '-i', voice_path,
        '-i', track_path,
        '-filter_complex', '[0:a][1:a]concat=n=2:v=0:a=1[out]',
        '-map', '[out]',
        '-c:a', 'libmp3lame',
        '-b:a', '128k',
        output_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0 and Path(output_path).exists():
            return True
        logger.error(f"FFmpeg merge error: {stderr.decode('utf-8', 'ignore')}")
        return False
    except Exception as e:
        logger.error(f"Merge execution failed: {e}")
        return False


def format_duration(seconds: int) -> str:
    mins, secs = divmod(seconds, 60)
    return f"{mins}:{secs:02d}"

def get_now_playing_message(track: TrackInfo, genre_name: str) -> str:
    icon = random.choice(["🎧", "🎵", "🎶", "📻", "💿"])
    safe_title = str(track.title).replace('*', '').replace('_', '').replace('[', '').replace(']', '').replace('`', '')
    safe_artist = str(track.artist).replace('*', '').replace('_', '').replace('[', '').replace(']', '').replace('`', '')
    safe_genre = str(genre_name).replace('*', '').replace('_', '').replace('[', '').replace(']', '').replace('`', '')
    return f"{icon} *{safe_title[:40].strip()}*\n👤 {safe_artist[:30].strip()}\n⏱ {format_duration(track.duration)} | 📻 _{safe_genre}_"

def get_random_catalog_query() -> tuple[str, Optional[str], str]:
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


from quiz_service import QuizManager


@dataclass
class RadioSession:
    chat_id: int
    bot: Bot
    downloader: YouTubeDownloader
    settings: Settings
    chat_manager: ChatManager
    quiz_manager: QuizManager  #  injected
    radio_manager: 'RadioManager' # injected
    query: str
    display_name: str
    chat_type: Optional[str] = None
    decade: Optional[str] = None
    
    is_running: bool = field(init=False, default=False)
    playlist: List[TrackInfo] = field(default_factory=list)
    played_ids: Set[str] = field(default_factory=set)
    current_task: Optional[asyncio.Task] = field(init=False, default=None)
    skip_event: asyncio.Event = field(default_factory=asyncio.Event)
    status_message: Optional[Message] = field(init=False, default=None)
    _is_searching: bool = field(init=False, default=False)
    
    last_genre_change: float = field(init=False, default_factory=time.time)
    failed_downloads_count: int = field(init=False, default=0) # ⚠️ СЧЕТЧИК ФЕЙЛОВ
    
    # 🔥 ПАРАМЕТРЫ ДЛЯ ВИКТОРИНЫ
    quiz_active: bool = field(init=False, default=False)
    quiz_artist: str = field(init=False, default="")
    quiz_title: str = field(init=False, default="")
    quiz_full: str = field(init=False, default="")
    last_quiz_time: float = field(init=False, default_factory=time.time)
    
    async def start(self):
        if self.is_running: return
        self.is_running = True
        self.current_task = asyncio.create_task(self._radio_loop())
        logger.info(f"[{self.chat_id}] 🚀 Эфир запущен: '{self.query}'")

    async def stop(self):
        self.is_running = False
        if self.current_task: self.current_task.cancel()
        self.quiz_active = False
        await self._delete_status()
        logger.info(f"[{self.chat_id}] 🛑 Эфир остановлен.")

    async def skip(self):
        self.skip_event.set()

    async def _handle_forbidden(self):
        self.is_running = False
        self.skip_event.set()

    async def _update_status(self, text: str):
        if not self.is_running: return
        try:
            if self.status_message:
                try: await self.status_message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
                except BadRequest: self.status_message = None
            if not self.status_message:
                self.status_message = await self.bot.send_message(self.chat_id, text, parse_mode=ParseMode.MARKDOWN)
        except Forbidden: await self._handle_forbidden()
        except Exception: self.status_message = None

    async def _delete_status(self):
        if self.status_message:
            try: await self.status_message.delete()
            except: pass
            self.status_message = None

    async def _fill_playlist(self, retry_query: str = None):
        if self._is_searching or not self.is_running: return
        self._is_searching = True
        base_query = retry_query or self.query
        variations = [base_query, f"{base_query} mix", f"{base_query} hits", f"best of {base_query}"]
        if random.random() > 0.5: variations.append(f"{base_query} 2024")
        random.shuffle(variations)
        
        found_new = False
        for q in variations:
            if not self.is_running: break
            try:
                tracks = await self.downloader.search(q, limit=15)
                new_tracks = [t for t in tracks if t.identifier not in self.played_ids]
                if new_tracks:
                    random.shuffle(new_tracks)
                    self.playlist.extend(new_tracks)
                    found_new = True
                    break
            except Exception as e:
                # 🟢 Теперь мы увидим, если YouTube отвалился по API лимитам
                logger.warning(f"Failed to fetch tracks for query '{q}': {e}")
        
        if not found_new:
            if len(self.played_ids) > 10:
                self.played_ids = set(list(self.played_ids)[-10:])
            else: self.played_ids.clear()
        self._is_searching = False


    async def _radio_loop(self):
        while self.is_running:
            try:
                # 🟢 Теперь радио просто смотрит в QuizManager. Никакой магии с флагами!
                if self.quiz_manager and self.quiz_manager.is_active(self.chat_id):
                    await asyncio.sleep(2)
                    continue

                # 🎮 АВТО-ВИКТОРИНА (убираем передачу self.radio_manager)
                if time.time() - self.last_quiz_time > 900:
                    self.last_quiz_time = time.time()
                    if self.quiz_manager:
                        logger.info(f"[{self.chat_id}] 🎮 Запуск авто-викторины по таймеру!")
                        asyncio.create_task(self.quiz_manager.start_quiz(self.chat_id, self.bot))
                        continue

                # 🔄 Ротация жанров (раз в час ИЛИ если слишком много фейлов скачивания)
                if time.time() - self.last_genre_change > 3600 or self.failed_downloads_count >= 5:
                    
                    if self.failed_downloads_count >= 5:
                        logger.warning(f"[{self.chat_id}] ⚠️ 5 неудачных скачиваний подряд. Принудительная смена жанра!")
                        self.failed_downloads_count = 0 
                    
                    from radio import get_random_catalog_query 
                    from ai_personas import PERSONAS 
                    new_query, new_decade, new_display_name = get_random_catalog_query()
                    self.query, self.decade, self.display_name = new_query, new_decade, new_display_name
                    self.playlist.clear()
                    self.last_genre_change = time.time()
                    
                    available_modes = list(PERSONAS.keys())
                    new_mode = random.choice(available_modes)
                    await self.chat_manager.set_mode(self.chat_id, new_mode)
                    
                    prompt = f"Прошел час. Я меняю музыкальную пластинку на жанр: '{self.display_name}'. А еще у меня внезапно сменилось настроение на 100%! Напиши классный, сбивающий с толку анонс об этом в чат в своем стиле."
                    announcement = await self.chat_manager.get_response(self.chat_id, prompt, "System")
                    if announcement:
                        try:
                            # 1. Очищаем текст от эмодзи (чтобы Аврора не читала вслух "смайлик огонь")
                            clean_text = re.sub(r'[^\w\s\.,!?\-а-яА-ЯёЁa-zA-Z]', '', announcement).strip()
                            
                            # 2. Путь для сохранения голосового сообщения
                            voice_path = self.settings.DOWNLOADS_DIR / f"dj_voice_{self.chat_id}_{int(time.time())}.ogg"
                            
                            # 3. Генерируем голос! 
                            # ru-RU-SvetlanaNeural - женский приятный голос. rate="+10%" - чуть бодрее.
                            communicate = edge_tts.Communicate(clean_text, "ru-RU-SvetlanaNeural", rate="+10%")
                            await communicate.save(str(voice_path))
                            
                            # 4. Отправляем как настоящее голосовое сообщение (войс)
                            if voice_path.exists():
                                with open(voice_path, 'rb') as f:
                                    await self.bot.send_voice(self.chat_id, voice=f)
                                os.unlink(voice_path) # Удаляем файл сразу после отправки
                            else:
                                raise FileNotFoundError("Voice file not created")
                                
                        except Exception as e:
                            logger.error(f"Voice generation failed: {e}")
                            # План Б: Если генерация голоса сломалась, просто шлем текстом
                            await self.bot.send_message(self.chat_id, f"🎙 {announcement}")
                    await asyncio.sleep(2)

                if len(self.playlist) < 3: await self._fill_playlist()
                if not self.playlist:
                    await self._update_status("📡 Поиск новой музыки...")
                    await asyncio.sleep(5)
                    await self._fill_playlist()
                    if not self.playlist:
                        self.failed_downloads_count += 1
                        await asyncio.sleep(5)
                        continue

                track = self.playlist.pop(0)

                await self._update_status(f"⬇️ Загрузка: {track.title[:20]}...")
                result = await self.downloader.download(track.identifier, track_info=track)
                
                is_valid_file = False
                if result and result.success:
                    if result.is_url or await self.downloader._cache.get(f"file_id:{track.identifier}"):
                        is_valid_file = True
                    elif result.file_path and Path(result.file_path).exists():
                        file_size_mb = Path(result.file_path).stat().st_size / (1024 * 1024)
                        if 1.0 <= file_size_mb <= 20.0: 
                            is_valid_file = True
                        else: 
                            logger.error(f"[{self.chat_id}] ❌ Трек отклонен из-за размера: {file_size_mb:.2f} MB.")
                            os.unlink(result.file_path)

                if not is_valid_file:
                    self.failed_downloads_count += 1
                    await self._delete_status()
                    continue

                self.failed_downloads_count = 0
                self.played_ids.add(track.identifier)
                if len(self.played_ids) > 500: self.played_ids = set(list(self.played_ids)[250:])

                disable_cache = False # По умолчанию кэш работает
                
                # Делаем склейку, только если трек реально скачался на диск
                if result and not result.is_url and result.file_path and Path(result.file_path).exists():
                    try:
                        topics = [
                            "смешную сплетню (можно выдуманную) про",
                            "какую-нибудь дикую историю с концерта",
                            "философскую мысль о том, как музыка влияет на людей, а затем упомяни",
                            "абсурдный факт про запись альбома",
                            "странную привычку музыкантов, а потом поставь",
                        ]
                        prompt = f"Ты радио-диджей. Расскажи {random.choice(topics)} артиста '{track.artist}'. Будь кратким (максимум 2-3 предложения)."
                        
                        announcement = await self.chat_manager.get_response(self.chat_id, prompt, "System")
                        if announcement:
                            # 1. Очищаем текст
                            clean_text = re.sub(r'[^\w\s\.,!?\-а-яА-ЯёЁa-zA-Z]', '', announcement).strip()
                            
                            # 2. Пути для файлов
                            voice_path = str(self.settings.DOWNLOADS_DIR / f"voice_{self.chat_id}.mp3")
                            merged_path = str(self.settings.DOWNLOADS_DIR / f"merged_{track.identifier}_{int(time.time())}.mp3")
                            
                            # 3. Генерируем голос
                            communicate = edge_tts.Communicate(clean_text, "ru-RU-SvetlanaNeural", rate="+10%")
                            await communicate.save(voice_path)
                            
                            # 4. СПАИВАЕМ ФАЙЛЫ!
                            if os.path.exists(voice_path):
                                is_merged = await merge_audio(voice_path, str(result.file_path), merged_path)
                                if is_merged:
                                    # Подменяем оригинальный трек на наш склеенный микс
                                    result.file_path = merged_path
                                    disable_cache = True # 🟢 ЗАПРЕЩАЕМ КЭШИРОВАТЬ ЭТОТ УНИКАЛЬНЫЙ МИКС!
                                
                                try: os.unlink(voice_path)
                                except: pass
                    except Exception as e:
                        logger.error(f"DJ Intro merge error: {e}")

                # Передаем флаг disable_cache в отправку
                success = await self._send_track(track, result, disable_cache=disable_cache)
                
                if success:
                    try: await asyncio.wait_for(self.skip_event.wait(), timeout=180.0)
                    except asyncio.TimeoutError: pass 
                else: await asyncio.sleep(2)
                
                self.skip_event.clear()
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"Loop error: {e}", exc_info=True); await asyncio.sleep(5)
        self.is_running = False

    async def _send_track(self, track: TrackInfo, result: DownloadResult, disable_cache: bool = False) -> bool:
        try:
            caption = get_now_playing_message(track, self.display_name)
            markup = None
            if self.chat_type != ChatType.CHANNEL:
                buttons = []
                player_url = getattr(self.settings, 'PLAYER_URL', '') or getattr(self.settings, 'BASE_URL', '') or getattr(self.settings, 'WEBHOOK_URL', '').replace('/telegram', '')
                if player_url: 
                    if not player_url.startswith('http'): player_url = f"https://{player_url}"
                    buttons.append(InlineKeyboardButton("▶️ Плеер", url=player_url))
                buttons.append(InlineKeyboardButton("⏭ Скип", callback_data="skip_track"))
                markup = InlineKeyboardMarkup([buttons])

            audio_source = result.file_path
            
            if result.is_url:
                await self.bot.send_audio(self.chat_id, audio=audio_source, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup, read_timeout=60, write_timeout=60)
                await self._delete_status()
                return True

            # 🟢 Проверяем кэш только если он не запрещен (нет уникальной подводки)
            if not disable_cache:
                cached_file_id = await self.downloader._cache.get(f"file_id:{track.identifier}")
                if cached_file_id:
                    try:
                        await self.bot.send_audio(self.chat_id, audio=cached_file_id, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup, read_timeout=60, write_timeout=60)
                        await self._delete_status()
                        return True
                    except Exception:
                        await self.downloader._cache.delete(f"file_id:{track.identifier}")

            if audio_source and Path(audio_source).exists():
                with open(audio_source, 'rb') as f:
                    # 🟢 Явно прописываем title и performer, чтобы скрыть склейку от пользователя
                    msg = await self.bot.send_audio(
                        self.chat_id, 
                        audio=f, 
                        caption=caption, 
                        title=track.title,
                        performer=track.artist,
                        parse_mode=ParseMode.MARKDOWN, 
                        reply_markup=markup, 
                        read_timeout=120, 
                        write_timeout=120
                    )
                    # Сохраняем в кэш только "чистые" треки без подводки
                    if msg.audio and not disable_cache: 
                        await self.downloader._cache.set(f"file_id:{track.identifier}", msg.audio.file_id, ttl=None)
                
                await self._delete_status()
                return True
            return False
            
        except Forbidden: 
            await self._handle_forbidden()
            return False
        except Exception as e: 
            logger.error(f"[{self.chat_id}] CRITICAL SEND ERROR: {e}", exc_info=True)
            return False

class RadioManager:
    def __init__(self, bot: Bot, settings: Settings, downloader: YouTubeDownloader, chat_manager: ChatManager, quiz_manager: QuizManager):
        self._bot, self._settings, self._downloader, self._chat_manager, self._quiz_manager = bot, settings, downloader, chat_manager, quiz_manager
        self._sessions: Dict[int, RadioSession] = {}
        self._locks: Dict[int, asyncio.Lock] = {}

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        self._locks.setdefault(chat_id, asyncio.Lock())
        return self._locks[chat_id]

    async def start(self, chat_id: int, query: str, chat_type: Optional[str] = None, display_name: Optional[str] = None, decade: Optional[str] = None):
        async with self._get_lock(chat_id):
            if chat_id in self._sessions: await self._sessions[chat_id].stop()
            
            if query == "random": 
                query, random_decade, random_display_name = get_random_catalog_query()
                if not decade: decade = random_decade
                if not display_name: display_name = random_display_name

            session = RadioSession(
                chat_id=chat_id, 
                bot=self._bot, 
                downloader=self._downloader, 
                settings=self._settings, 
                chat_manager=self._chat_manager,
                quiz_manager=self._quiz_manager,
                radio_manager=self,
                query=query, 
                display_name=(display_name or query), 
                decade=decade, 
                chat_type=chat_type
            )
            self._sessions[chat_id] = session
            await session.start()

    async def stop(self, chat_id: int):
        async with self._get_lock(chat_id):
            if session := self._sessions.pop(chat_id, None): await session.stop()

    async def skip(self, chat_id: int):
        if session := self._sessions.get(chat_id): await session.skip()

    async def stop_all(self):
        tasks = [self.stop(cid) for cid in list(self._sessions.keys())]
        if tasks: await asyncio.gather(*tasks)
