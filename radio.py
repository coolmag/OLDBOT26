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
from telegram.error import BadRequest, Forbidden, RetryAfter

from config import Settings
from models import TrackInfo, DownloadResult
from youtube import YouTubeDownloader
from chat_service import ChatManager
from ai_personas import PERSONAS

with open(Path(__file__).parent / "genres.json", "r", encoding="utf-8") as f:
    MUSIC_CATALOG = json.load(f)

logger = logging.getLogger(__name__)


async def merge_audio(voice_path: str, track_path: str, output_path: str) -> bool:
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
    return f"{icon} *{safe_title[:40].strip()}* | 👤 {safe_artist[:30].strip()} | ⏱ {format_duration(track.duration)} | 📻 _{safe_genre}_"

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
    quiz_manager: QuizManager
    radio_manager: 'RadioManager'
    query: str
    display_name: str
    chat_type: Optional[str] = None
    decade: Optional[str] = None
    
    is_running: bool = field(init=False, default=False)
    is_temporary_mode: bool = field(init=False, default=False)
    playlist: List[TrackInfo] = field(default_factory=list)
    played_ids: Set[str] = field(default_factory=set)
    current_task: Optional[asyncio.Task] = field(init=False, default=None)
    skip_event: asyncio.Event = field(default_factory=asyncio.Event)
    status_message: Optional[Message] = field(init=False, default=None)
    _is_searching: bool = field(init=False, default=False)
    
    last_genre_change: float = field(init=False, default_factory=time.time)
    failed_downloads_count: int = field(init=False, default=0)
    
    quiz_active: bool = field(init=False, default=False)
    quiz_artist: str = field(init=False, default="")
    quiz_title: str = field(init=False, default="")
    quiz_full: str = field(init=False, default="")
    last_quiz_time: float = field(init=False, default_factory=time.time)

    async def _send_telegram_message_with_retry(self, func, *args, **kwargs):
        while True:
            try:
                return await func(*args, **kwargs)
            except RetryAfter as e:
                logger.warning(f"[{self.chat_id}] Flood control exceeded. Retrying in {e.retry_after} seconds.")
                await asyncio.sleep(e.retry_after)
            except Exception as e:
                raise e # Re-raise other exceptions
    
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

    async def set_temporary_query(self, query: str, display_name: str):
        logger.info(f"[{self.chat_id}] 🎵 Установлен временный режим: '{display_name}'")
        self.query = query
        self.display_name = display_name
        self.is_temporary_mode = True
        self.playlist.clear()
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

        # START of new playlist logic
        genre_node = None
        for category in MUSIC_CATALOG.values():
            for key, genre in category.get("children", {}).items():
                if genre.get("name") == self.display_name:
                    genre_node = genre
                    break
            if genre_node:
                break
        
        if genre_node and "tracks" in genre_node:
            logger.info(f"[{self.chat_id}] 🎶 Filling playlist from static list: {self.display_name}")
            
            # Берем случайные 15 треков из списка, если он большой, или все, если маленький
            sample_size = min(15, len(genre_node["tracks"]))
            track_names_to_search = random.sample(genre_node["tracks"], sample_size)
            
            found_tracks = []
            for track_name in track_names_to_search:
                try:
                    # Ищем каждый трек отдельно, чтобы получить TrackInfo
                    search_results = await self.downloader.search(track_name, limit=1)
                    if search_results:
                        track_info = search_results[0]
                        if track_info.identifier not in self.played_ids:
                            found_tracks.append(track_info)
                except Exception as e:
                    logger.warning(f"Failed to search for static track '{track_name}': {e}")
            
            if found_tracks:
                random.shuffle(found_tracks)
                self.playlist.extend(found_tracks)
            
            self._is_searching = False
            return
        # END of new playlist logic

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
                logger.warning(f"Failed to fetch tracks for query '{q}': {e}")
        
        if not found_new:
            if len(self.played_ids) > 10:
                self.played_ids = set(list(self.played_ids)[-10:])
            else: self.played_ids.clear()
        self._is_searching = False

    async def _radio_loop(self):
        while self.is_running:
            try:
                if self.quiz_manager and self.quiz_manager.is_active(self.chat_id):
                    await asyncio.sleep(2)
                    continue

                if time.time() - self.last_quiz_time > 3600:
                    self.last_quiz_time = time.time()
                    if self.quiz_manager:
                        logger.info(f"[{self.chat_id}] 🎮 Запуск авто-викторины по таймеру!")
                        await self.quiz_manager.start_quiz(self.chat_id, self.bot)
                        await asyncio.sleep(5)
                        continue
                
                time_for_rotation = time.time() - self.last_genre_change > 3600
                too_many_failures = not self.is_temporary_mode and self.failed_downloads_count >= 5

                if time_for_rotation or too_many_failures:
                    if too_many_failures:
                        logger.warning(f"[{self.chat_id}] ⚠️ 5 неудачных скачиваний подряд. Принудительная смена жанра!")
                    
                    self.is_temporary_mode = False
                    self.failed_downloads_count = 0 
                    
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
                            clean_text = re.sub(r'[^\w\s\.,!?\-а-яА-ЯёЁa-zA-Z]', '', announcement).strip()
                            voice_path = self.settings.DOWNLOADS_DIR / f"dj_voice_{self.chat_id}_{int(time.time())}.ogg"
                            communicate = edge_tts.Communicate(clean_text, "ru-RU-SvetlanaNeural", rate="+10%")
                            await communicate.save(str(voice_path))
                            
                            if voice_path.exists():
                                with open(voice_path, 'rb') as f:
                                    await self._send_telegram_message_with_retry(self.bot.send_voice, self.chat_id, voice=f)
                                os.unlink(voice_path)
                            else:
                                raise FileNotFoundError("Voice file not created")
                        except Exception as e:
                            logger.error(f"Voice generation failed: {e}")
                            await self._send_telegram_message_with_retry(self.bot.send_message, self.chat_id, f"🎙 {announcement}")
                    await asyncio.sleep(2)

                if len(self.playlist) < 3: await self._fill_playlist()
                if not self.playlist:
                    await self._update_status("📡 Поиск новой музыки...")
                    self.failed_downloads_count += 1
                    logger.warning(f"[{self.chat_id}] Playlist is empty. Incrementing failure count to {self.failed_downloads_count}.")
                    await asyncio.sleep(10) # Даем время перед следующей попыткой или сменой жанра
                    continue

                track = self.playlist.pop(0)

                await self._update_status(f"⬇️ Загрузка: {track.title[:20]}...")
                try:
                    logger.info(f"[{self.chat_id}] Calling downloader for track: {track.title} ({track.identifier})")
                    # Таймаут в 3 минуты (180 секунд) на всю операцию скачивания
                    result = await asyncio.wait_for(
                        self.downloader.download(track.identifier, track_info=track),
                        timeout=180.0
                    )
                except asyncio.TimeoutError:
                    logger.error(f"[{self.chat_id}] TIMEOUT: Download for {track.title} took too long.")
                    result = None
                except Exception as e:
                    logger.error(f"[{self.chat_id}] UNCAUGHT exception during download call: {e}", exc_info=True)
                    result = None
                
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

                disable_cache = False
                
                if self.settings.ENABLE_AI_DJ_INTRO and result and not result.is_url and result.file_path and Path(result.file_path).exists():
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
                            clean_text = re.sub(r'[^\w\s\.,!?\-а-яА-ЯёЁa-zA-Z]', '', announcement).strip()
                            voice_path = str(self.settings.DOWNLOADS_DIR / f"voice_{self.chat_id}.mp3")
                            merged_path = str(self.settings.DOWNLOADS_DIR / f"merged_{track.identifier}_{int(time.time())}.mp3")
                            
                            communicate = edge_tts.Communicate(clean_text, "ru-RU-SvetlanaNeural", rate="+10%")
                            await communicate.save(voice_path)
                            
                            if os.path.exists(voice_path):
                                is_merged = await merge_audio(voice_path, str(result.file_path), merged_path)
                                if is_merged:
                                    result.file_path = merged_path
                                    disable_cache = True
                                
                                try: os.unlink(voice_path)
                                except: pass
                    except Exception as e:
                        logger.error(f"DJ Intro merge error: {e}")

                success = await self._send_track(track, result, disable_cache=disable_cache)
                
                if success and track.duration > 0:
                    try:
                        # Wait for track to finish (duration + 5s buffer), or until skip is called
                        await asyncio.wait_for(self.skip_event.wait(), timeout=float(track.duration + 5))
                    except asyncio.TimeoutError:
                        pass  # This is the expected behavior for a track finishing
                elif success: # track has no duration, use default
                    try: await asyncio.wait_for(self.skip_event.wait(), timeout=360.0)
                    except asyncio.TimeoutError: pass
                else: # Sending failed
                    await asyncio.sleep(2)
                
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

    async def set_genre(self, chat_id: int, genre: str):
        if session := self._sessions.get(chat_id):
            if session.is_running:
                await session.set_temporary_query(genre, f"жанр: {genre}")
                return True
        return False

    async def set_artist(self, chat_id: int, artist: str):
        if session := self._sessions.get(chat_id):
            if session.is_running:
                await session.set_temporary_query(artist, f"исполнитель: {artist}")
                return True
        return False

    async def start(self, chat_id: int, query: str, chat_type: Optional[str] = None, display_name: Optional[str] = None, decade: Optional[str] = None):
        async with self._get_lock(chat_id):
            # Если сессия уже существует и активна, обновляем её, не останавливая
            if (session := self._sessions.get(chat_id)) and session.is_running:
                final_query = query
                final_display_name = display_name or query
                
                if query == "random": 
                    random_query, random_decade, random_display_name = get_random_catalog_query()
                    final_query = random_query
                    final_display_name = random_display_name
                    if not decade: decade = random_decade # Обновляем decade, если не задан

                logger.info(f"[{chat_id}] 🔄 Смена волны с '{session.display_name}' на '{final_display_name}'")
                await session.set_temporary_query(final_query, final_display_name)
                session.decade = decade # Обновляем decade в сессии
                session.is_temporary_mode = False # Теперь это не временный, а основной режим
                return # Выходим, существующая сессия продолжит работу с новой волной

            # Иначе (если сессии нет или она неактивна), создаем новую, как раньше
            if chat_id in self._sessions: # Очищаем неактивную или зависшую сессию, если есть
                await self._sessions[chat_id].stop()
            
            final_query = query
            final_display_name = display_name or query
            final_decade = decade

            if query == "random": 
                random_query, random_decade, random_display_name = get_random_catalog_query()
                final_query = random_query
                final_display_name = random_display_name
                if not final_decade: 
                    final_decade = random_decade

            session = RadioSession(
                chat_id=chat_id, 
                bot=self._bot, 
                downloader=self._downloader, 
                settings=self._settings, 
                chat_manager=self._chat_manager,
                quiz_manager=self._quiz_manager,
                radio_manager=self,
                query=final_query, 
                display_name=final_display_name,
                decade=final_decade, 
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
