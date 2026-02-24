import asyncio
import logging
import random
import os
import time
import json
from pathlib import Path
from typing import List, Optional, Dict, Set
from dataclasses import dataclass, field
from urllib.parse import urlparse

from telegram import Bot, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest, Forbidden

from config import Settings
from models import TrackInfo, DownloadResult
from youtube import YouTubeDownloader
from chat_service import ChatManager

with open(Path(__file__).parent / "genres.json", "r", encoding="utf-8") as f:
    MUSIC_CATALOG = json.load(f)

logger = logging.getLogger("radio")

def format_duration(seconds: int) -> str:
    mins, secs = divmod(seconds, 60)
    return f"{mins}:{secs:02d}"

def get_now_playing_message(track: TrackInfo, genre_name: str) -> str:
    icon = random.choice(["🎧", "🎵", "🎶", "📻", "💿"])
    # Очищаем текст от символов, которые ломают Telegram Markdown
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

@dataclass
class RadioSession:
    chat_id: int
    bot: Bot
    downloader: YouTubeDownloader
    settings: Settings
    chat_manager: ChatManager
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
    last_genre_change: float = field(init=False, default_factory=time.time)
    
    async def start(self):
        if self.is_running: return
        self.is_running = True
        self.current_task = asyncio.create_task(self._radio_loop())
        logger.info(f"[{self.chat_id}] 🚀 Эфир запущен: '{self.query}'")

    async def stop(self):
        self.is_running = False
        if self.current_task: self.current_task.cancel()
        await self._delete_status()

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
            except Exception: pass
        
        if not found_new:
            if len(self.played_ids) > 10:
                self.played_ids = set(list(self.played_ids)[-10:])
            else: self.played_ids.clear()
        self._is_searching = False

    async def _radio_loop(self):
        while self.is_running:
            try:
                # 🔄 АВТО-РОТАЦИЯ ЖАНРОВ РАЗ В ЧАС (3600 секунд)
                if time.time() - self.last_genre_change > 3600:
                    new_query, new_decade, new_display_name = get_random_catalog_query()
                    self.query, self.decade, self.display_name = new_query, new_decade, new_display_name
                    self.playlist.clear()
                    self.last_genre_change = time.time()
                    
                    # ИИ анонсирует смену жанра
                    prompt = f"Прошел час. Я меняю музыкальную пластинку на жанр: '{self.display_name}'. Напиши короткий стильный анонс об этом в чат."
                    announcement = await self.chat_manager.get_response(self.chat_id, prompt, "System")
                    await self.bot.send_message(self.chat_id, f"🎙 {announcement}")
                    await asyncio.sleep(2)

                if len(self.playlist) < 3: await self._fill_playlist()
                if not self.playlist:
                    await self._update_status("📡 Поиск новой музыки...")
                    await asyncio.sleep(5)
                    await self._fill_playlist()
                    if not self.playlist:
                        await asyncio.sleep(10)
                        continue

                track = self.playlist.pop(0)
                self.played_ids.add(track.identifier)
                if len(self.played_ids) > 500: self.played_ids = set(list(self.played_ids)[250:])

                success = await self._play_track(track)
                
                if success:
                    wait_time = min(track.duration, 300) if track.duration > 0 else 180
                    try: await asyncio.wait_for(self.skip_event.wait(), timeout=wait_time)
                    except asyncio.TimeoutError: pass 
                else: await asyncio.sleep(2)
                
                self.skip_event.clear()
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"Loop error: {e}"); await asyncio.sleep(5)
        self.is_running = False

    async def _play_track(self, track: TrackInfo) -> bool:
        result = None
        if not self.is_running: return False
        try:
            await self._update_status(f"⬇️ Загрузка: *{track.title[:20]}*...")
            result = await self.downloader.download(track.identifier, track_info=track)
            
            if not result or not result.success: return False
            
            caption = get_now_playing_message(track, self.display_name)
            
            # 🎛 КНОПКИ ПЛЕЕРА И СКИПА
            markup = None
            if self.chat_type != ChatType.CHANNEL:
                buttons = []
                player_url = getattr(self.settings, 'PLAYER_URL', '') or getattr(self.settings, 'BASE_URL', '') or getattr(self.settings, 'WEBHOOK_URL', '').replace('/telegram', '')
                if player_url: 
                    if not player_url.startswith('http'): player_url = f"https://{player_url}"
                    # Безопасная кнопка для групп
                    buttons.append(InlineKeyboardButton("▶️ Плеер", url=player_url))
                buttons.append(InlineKeyboardButton("⏭ Скип", callback_data="skip_track"))
                markup = InlineKeyboardMarkup([buttons])

            audio_source = result.file_path
            
            if result.is_url:
                await self.bot.send_audio(self.chat_id, audio=str(audio_source), caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup, read_timeout=60, write_timeout=60)
                await self._delete_status()
                return True

            cached_file_id = await self.downloader._cache.get(f"file_id:{track.identifier}")
            if cached_file_id:
                try:
                    await self.bot.send_audio(self.chat_id, audio=cached_file_id, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup, read_timeout=60, write_timeout=60)
                    await self._delete_status()
                    return True
                except Exception as e:
                    logger.warning(f"Failed to send cached file_id: {e}")
                    await self.downloader._cache.delete(f"file_id:{track.identifier}")

            if audio_source and Path(audio_source).exists():
                with open(audio_source, 'rb') as f:
                    # Добавлены таймауты, чтобы Railway не обрывал отправку больших файлов
                    msg = await self.bot.send_audio(self.chat_id, audio=f, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup, read_timeout=120, write_timeout=120)
                    if msg.audio: await self.downloader._cache.set(f"file_id:{track.identifier}", msg.audio.file_id, ttl=None)
                await self._delete_status()
                return True
                
            return False
            
        except Forbidden: 
            await self._handle_forbidden()
            return False
        except Exception as e: 
            # ТЕПЕРЬ МЫ УВИДИМ ОШИБКУ В ЛОГАХ ЕСЛИ ЧТО-ТО ПОЙДЕТ НЕ ТАК
            logger.error(f"[{self.chat_id}] CRITICAL SEND ERROR: {e}", exc_info=True)
            return False
        finally:
            if result and not result.is_url and result.file_path and Path(result.file_path).exists():
                try: os.unlink(result.file_path)
                except: pass

class RadioManager:
    def __init__(self, bot: Bot, settings: Settings, downloader: YouTubeDownloader, chat_manager: ChatManager):
        self._bot, self._settings, self._downloader, self._chat_manager = bot, settings, downloader, chat_manager
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
                chat_id=chat_id, bot=self._bot, downloader=self._downloader, 
                settings=self._settings, chat_manager=self._chat_manager,
                query=query, display_name=(display_name or query), 
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
