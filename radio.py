import asyncio
import logging  # ⚠️ ЭТОТ ИМПОРТ ДОЛЖЕН БЫТЬ
import random
import os
import time
import json
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
    current_task: Optional[asyncio.Task] = field(init=False, default=None)
    skip_event: asyncio.Event = field(default_factory=asyncio.Event)
    status_message: Optional[Message] = field(init=False, default=None)
    _is_searching: bool = field(init=False, default=False)
    
    last_genre_change: float = field(init=False, default_factory=time.time)
    
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
            except Exception: pass
        
        if not found_new:
            if len(self.played_ids) > 10:
                self.played_ids = set(list(self.played_ids)[-10:])
            else: self.played_ids.clear()
        self._is_searching = False

    # 🎮 ЛОГИКА АВТОМАТИЧЕСКОЙ ВИКТОРИНЫ (Бронебойная)
    async def run_quiz(self):
        # ⚠️ Инициализируем переменные заранее, чтобы исключить UnboundLocalError
        dl_res = None
        input_file = None
        output_file = None
        
        try:
            self.quiz_active = True
            await self._update_status("🎲 <i>Настраиваю аппаратуру для викторины...</i>")
            
            queries = ["хиты 2000х", "руки вверх", "король и шут", "linkin park", "eminem", "macan", "miyagi", "баста", "anna asti", "queen", "nirvana", "t.a.t.u.", "моргенштерн", "сектор газа", "zivert", "скриптонит"]
            tracks = await self.downloader.search(random.choice(queries), limit=5)
            if not tracks:
                self.quiz_active = False
                return
                
            track = random.choice(tracks[:3])
            dl_res = await self.downloader.download(track.identifier, track)
            if not dl_res or not dl_res.success or not dl_res.file_path:
                self.quiz_active = False
                return
                
            info = dl_res.track_info
            input_file = str(dl_res.file_path)
            output_file = str(self.settings.DOWNLOADS_DIR / f"quiz_{track.identifier}.ogg")
            start_time = max(0, (info.duration // 2) - 10) if info.duration else 30
            
            cmd = ['ffmpeg', '-y', '-i', input_file, '-ss', str(start_time), '-t', '15', '-c:a', 'copy', output_file]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            
            if not os.path.exists(output_file) or os.path.getsize(output_file) == 0: 
                cmd_fallback = ['ffmpeg', '-y', '-i', input_file, '-ss', str(start_time), '-t', '15', output_file]
                proc2 = await asyncio.create_subprocess_exec(*cmd_fallback, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await proc2.wait()
                
            await self._delete_status()
            
            prompt = "Ты ведешь игру 'Угадай мелодию'. Коротко и очень энергично скажи: 'Слушаем 15 секунд! Кто первый напишет название трека или артиста в чат — тот заберет очки. Время пошло!'"
            announcement = await self.chat_manager.get_response(self.chat_id, prompt, "System")
            if announcement: 
                await self.bot.send_message(self.chat_id, f"🎙 {announcement}")
                
            with open(output_file, 'rb') as f:
                await self.bot.send_voice(self.chat_id, voice=f)
                
            self.quiz_artist = info.artist
            self.quiz_title = info.title
            self.quiz_full = f"{info.artist} - {info.title}"
            
            await asyncio.sleep(30)
            
            if self.quiz_active:
                self.quiz_active = False
                prompt = f"Время вышло, никто не угадал песню! Это был трек: {self.quiz_full}. Высмей их музыкальный вкус в своем стиле."
                roast = await self.chat_manager.get_response(self.chat_id, prompt, "System")
                await self.bot.send_message(self.chat_id, f"⏰ 🎙 {roast}", parse_mode=ParseMode.MARKDOWN)
                
        except Exception as e:
            logger.error(f"Quiz run error: {e}")
            self.quiz_active = False
        finally:
            # ⚠️ Безопасное удаление файлов
            if dl_res and getattr(dl_res, 'is_url', False) == False and input_file and os.path.exists(input_file): 
                try: os.unlink(input_file)
                except: pass
            if output_file and os.path.exists(output_file): 
                try: os.unlink(output_file)
                except: pass

    async def _radio_loop(self):
        while self.is_running:
            try:
                if self.quiz_active:
                    await asyncio.sleep(2)
                    continue

                if time.time() - self.last_quiz_time > 1800:
                    self.last_quiz_time = time.time()
                    await self.run_quiz()
                    continue

                if time.time() - self.last_genre_change > 3600:
                    # ⚠️ Убрали само-импорты (вызываем функцию напрямую)
                    from ai_personas import PERSONAS 
                    
                    new_query, new_decade, new_display_name = get_random_catalog_query()
                    self.query, self.decade, self.display_name = new_query, new_decade, new_display_name
                    self.playlist.clear()
                    self.last_genre_change = time.time()
                    
                    available_modes = list(PERSONAS.keys())
                    new_mode = random.choice(available_modes)
                    self.chat_manager.set_mode(self.chat_id, new_mode)
                    
                    prompt = f"Прошел час. Я меняю музыкальную пластинку на жанр: '{self.display_name}'. А еще у меня внезапно сменилось настроение на 100%! Напиши классный, сбивающий с толку анонс об этом в чат в своем стиле."
                    announcement = await self.chat_manager.get_response(self.chat_id, prompt, "System")
                    if announcement:
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

                await self._update_status(f"⬇️ Загрузка: {track.title[:20]}...")
                result = await self.downloader.download(track.identifier, track_info=track)
                
                is_valid_file = False
                if result and result.success:
                    if result.is_url or await self.downloader._cache.get(f"file_id:{track.identifier}"):
                        is_valid_file = True
                    elif result.file_path and Path(result.file_path).exists():
                        file_size_mb = Path(result.file_path).stat().st_size / (1024 * 1024)
                        if file_size_mb <= 20.0: is_valid_file = True
                        else: os.unlink(result.file_path)

                if not is_valid_file:
                    await self._delete_status()
                    continue

                self.played_ids.add(track.identifier)
                if len(self.played_ids) > 500: self.played_ids = set(list(self.played_ids)[250:])

                try:
                    topics = [
                        "смешную сплетню (можно выдуманную) про",
                        "какую-нибудь дикую историю с концерта",
                        "философскую мысль о том, как музыка влияет на людей, а затем упомяни",
                        "абсурдный и нелепый факт про запись альбома",
                        "странную привычку музыкантов, а потом поставь",
                        "короткий музыкальный анекдот, а в конце подведи к",
                        "что-то про космические корабли, инопланетян и как это связано с песней",
                        "дерзкую шутку про музыкальных критиков, а затем включи"
                    ]
                    random_topic = random.choice(topics)
                    prompt = f"""Ты в прямом эфире радио! Твоя задача: Расскажи {random_topic} артиста '{track.artist}' или трека '{track.title}'.
                    ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:
                    1. Отвечай СТРОГО в стиле своей текущей личности.
                    2. СТРОГИЙ ЗАПРЕТ на фразы: "свидание вслепую", "экзистенциальный ужас", "бессонница". ЗАБУДЬ ИХ!
                    3. Придумай что-то абсолютно новое, креативное и безумное.
                    4. Будь кратким (максимум 2-3 предложения)."""
                    
                    announcement = await self.chat_manager.get_response(self.chat_id, prompt, "System")
                    if announcement:
                        await self.bot.send_message(self.chat_id, f"🎙 {announcement}")
                except Exception as e:
                    logger.error(f"DJ Intro error: {e}")

                success = await self._send_track(track, result)
                
                if success:
                    try: await asyncio.wait_for(self.skip_event.wait(), timeout=180.0)
                    except asyncio.TimeoutError: pass 
                else: await asyncio.sleep(2)
                
                self.skip_event.clear()
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"Loop error: {e}"); await asyncio.sleep(5)
        self.is_running = False

    async def _send_track(self, track: TrackInfo, result: DownloadResult) -> bool:
        try:
            # ⚠️ Вызываем напрямую, без само-импортов
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
                    msg = await self.bot.send_audio(self.chat_id, audio=f, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=markup, read_timeout=120, write_timeout=120)
                    if msg.audio: await self.downloader._cache.set(f"file_id:{track.identifier}", msg.audio.file_id, ttl=None)
                await self._delete_status()
                return True
            return False
            
        except Forbidden: 
            await self._handle_forbidden()
            return False
        except Exception as e: 
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
