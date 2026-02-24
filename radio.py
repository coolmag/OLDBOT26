import asyncio
import logging
import random
import os
from typing import List, Optional, Dict, Set
from dataclasses import dataclass, field
from urllib.parse import urlparse

from telegram import Bot, Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest, RetryAfter, Forbidden

from config import Settings
from models import TrackInfo, DownloadResult
from youtube import YouTubeDownloader
import json
from pathlib import Path

# Load MUSIC_CATALOG from genres.json
with open(Path(__file__).parent / "genres.json", "r", encoding="utf-8") as f:
    MUSIC_CATALOG = json.load(f)

logger = logging.getLogger("radio")

def format_duration(seconds: int) -> str:
    mins, secs = divmod(seconds, 60)
    return f"{mins}:{secs:02d}"

def get_now_playing_message(track: TrackInfo, genre_name: str) -> str:
    icon = random.choice(["🎧", "🎵", "🎶", "📻", "💿"])
    title = track.title[:40].strip()
    artist = track.artist[:30].strip()
    return f"{icon} *{title}*\n👤 {artist}\n⏱ {format_duration(track.duration)} | 📻 _{genre_name}_"

@dataclass
class RadioSession:
    chat_id: int
    bot: Bot
    downloader: YouTubeDownloader
    settings: Settings
    query: str
    display_name: str
    chat_type: Optional[str] = None
    decade: Optional[str] = None
    
    is_running: bool = field(init=False, default=False)
    playlist: List[TrackInfo] = field(default_factory=list)
    played_ids: Set[str] = field(default_factory=set)
    current_task: Optional[asyncio.Task] = None
    skip_event: asyncio.Event = field(default_factory=asyncio.Event)
    status_message: Optional[Message] = None
    _is_searching: bool = field(init=False, default=False)
    
    async def start(self):
        if self.is_running: return
        self.is_running = True
        self.current_task = asyncio.create_task(self._radio_loop())
        logger.info(f"[{self.chat_id}] 🚀 Эфир запущен: '{self.query}'")

    async def stop(self):
        self.is_running = False
        if self.current_task: self.current_task.cancel()
        await self._delete_status()
        logger.info(f"[{self.chat_id}] 🛑 Эфир остановлен.")

    async def skip(self):
        self.skip_event.set()

    async def _handle_forbidden(self):
        logger.error(f"[{self.chat_id}] ⛔️ Бот заблокирован. Стоп.")
        self.is_running = False
        self.skip_event.set()

    async def _update_status(self, text: str):
        if not self.is_running: return
        try:
            if self.status_message:
                try: await self.status_message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
                except BadRequest: self.status_message = None
            
            if not self.status_message:
                self.status_message = await self.bot.send_message(self.chat_id, text, parse_mode=ParseMode.MARKDOWN, disable_notification=True)
        except Forbidden: await self._handle_forbidden()
        except Exception as e: 
            logger.warning(f"[{self.chat_id}] Failed to update status message: {e}")
            self.status_message = None

    async def _delete_status(self):
        if self.status_message:
            try: await self.status_message.delete()
            except: pass
            self.status_message = None

    async def _fill_playlist(self, retry_query: str = None):
        if self._is_searching or not self.is_running: return
        self._is_searching = True
        base_query = retry_query or self.query
        variations = [base_query, f"{base_query} mix", f"{base_query} best songs", f"{base_query} playlist", f"{base_query} hits", f"best of {base_query}"]
        if random.random() > 0.5:
            variations.append(f"{base_query} {random.choice(['2020s', '2010s', '2000s', '90s'])}")
        random.shuffle(variations)
        
        found_new = False
        for q in variations:
            if not self.is_running: break
            try:
                tracks = await self.downloader.search(q, limit=20)
                if not tracks: continue
                new_tracks = [t for t in tracks if t.identifier not in self.played_ids]
                if new_tracks:
                    random.shuffle(new_tracks)
                    self.playlist.extend(new_tracks)
                    logger.info(f"[{self.chat_id}] Найдено {len(new_tracks)} новых треков по запросу '{q}'.")
                    found_new = True
                    break
            except Exception as e:
                logger.error(f"Search error for {q}: {e}")
        
        if not found_new:
            logger.warning(f"[{self.chat_id}] Музыка кончилась. Сбрасываем историю прослушивания (частично).")
            if len(self.played_ids) > 10:
                self.played_ids = set(list(self.played_ids)[-10:])
            else:
                 self.played_ids.clear()
        self._is_searching = False

    async def _radio_loop(self):
        while self.is_running:
            try:
                if len(self.playlist) < 5: await self._fill_playlist()
                
                if not self.playlist:
                    await self._update_status("📡 Поиск новой музыки...")
                    await asyncio.sleep(5)
                    await self._fill_playlist()
                    if not self.playlist:
                        await self._update_status("🔍 Не удалось найти новую музыку. Повтор старой или ожидание...")
                        await asyncio.sleep(10)
                        continue

                track = self.playlist.pop(0)
                self.played_ids.add(track.identifier)
                if len(self.played_ids) > 500: self.played_ids = set(list(self.played_ids)[250:])

                success = await self._play_track(track)
                
                if success:
                    wait_time = min(track.duration, self.settings.MAX_TRACK_DURATION_S) if track.duration > 0 else 180
                    try: await asyncio.wait_for(self.skip_event.wait(), timeout=wait_time)
                    except asyncio.TimeoutError: pass 
                else: await asyncio.sleep(2)
                
                self.skip_event.clear()
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"Loop error: {e}", exc_info=True); await asyncio.sleep(5)
        self.is_running = False

    async def _play_track(self, track: TrackInfo) -> bool:
        result: Optional[DownloadResult] = None
        if not self.is_running: return False
        try:
            await self._update_status(f"⬇️ Загрузка: *{track.title}*...")
            result = await self.downloader.download(track.identifier, track_info=track)
            
            if not result or not result.success:
                logger.warning(f"[{self.chat_id}] Download failed for {track.identifier}.")
                return False
            
            caption = get_now_playing_message(track, self.display_name)
            markup = None
            
            player_url = getattr(self.settings, 'PLAYER_URL', getattr(self.settings, 'BASE_URL', ''))
            if not player_url and self.settings.WEBHOOK_URL:
                try:
                    parsed_webhook = urlparse(self.settings.WEBHOOK_URL)
                    player_url = f"{parsed_webhook.scheme}://{parsed_webhook.netloc}"
                    logger.info(f"[{self.chat_id}] Derived Player URL from webhook: '{player_url}'")
                except Exception:
                    logger.warning("Could not derive player URL from webhook.")

            logger.info(f"[{self.chat_id}] Checking for Player URL. Found: '{player_url}'")

            if self.chat_type != ChatType.CHANNEL:
                buttons = []
                if player_url:
                    buttons.append(InlineKeyboardButton("▶️ Плеер", web_app=WebAppInfo(url=player_url)))
                buttons.append(InlineKeyboardButton("⏭ Скип", callback_data="skip_track"))
                markup = InlineKeyboardMarkup([buttons])

            audio_source = result.file_path
            
            if result.is_url:
                await self.bot.send_audio(self.chat_id, audio=str(audio_source), caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup, timeout=120)
            else:
                cached_file_id = await self._cache.get(f"file_id:{track.identifier}")
                if cached_file_id:
                    try:
                        await self.bot.send_audio(self.chat_id, audio=cached_file_id, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
                    except:
                        await self._cache.delete(f"file_id:{track.identifier}")
                        cached_file_id = None
                
                if not cached_file_id and audio_source and Path(audio_source).exists():
                    with open(audio_source, 'rb') as f:
                        msg = await self.bot.send_audio(self.chat_id, audio=f, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
                        if msg.audio:
                            await self._cache.set(f"file_id:{track.identifier}", msg.audio.file_id, ttl=None)
            
            await self._delete_status()
            return True
        except Exception as e:
            logger.error(f"[{self.chat_id}] Critical error in _play_track for {track.identifier}: {e}", exc_info=True)
            return False
        finally:
            if result and not result.is_url and result.file_path and Path(result.file_path).exists():
                try: os.unlink(result.file_path)
                except Exception: pass

class RadioManager:
    def __init__(self, bot: Bot, settings: Settings, downloader: YouTubeDownloader):
        self._bot, self._settings, self._downloader = bot, settings, downloader
        self._sessions: Dict[int, RadioSession] = {}
        self._locks: Dict[int, asyncio.Lock] = {}

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        self._locks.setdefault(chat_id, asyncio.Lock())
        return self._locks[chat_id]

    async def start(self, chat_id: int, query: str, chat_type: Optional[str] = None, display_name: Optional[str] = None, decade: Optional[str] = None):
        async with self._get_lock(chat_id):
            if chat_id in self._sessions: await self._sessions[chat_id].stop()
            
            if query == "random": 
                query, random_decade, random_display_name = self._get_random_query()
                decade = decade or random_decade
                display_name = display_name or random_display_name

            session = RadioSession(
                chat_id=chat_id, bot=self._bot, downloader=self._downloader, 
                settings=self._settings, query=query, display_name=(display_name or query), 
                decade=decade, chat_type=chat_type
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

    def _get_random_query(self) -> tuple[str, Optional[str], str]:
        all_queries = []
        def extract(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, dict):
                        if "query" in v: 
                            all_queries.append((v["query"], v.get("decade"), v.get("name", k)))
                        elif "children" in v: extract(v["children"])
                    elif isinstance(v, list):
                        for item in v: extract(item)
        extract(MUSIC_CATALOG)
        
        return random.choice(all_queries) if all_queries else ("top hits", None, "Random")
