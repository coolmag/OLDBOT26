import asyncio
import logging
import subprocess
import difflib
import time
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
import yt_dlp
from ytmusicapi import YTMusic

from config import Settings
from models import DownloadResult, TrackInfo, Source
from cache_service import CacheService
from db_service import DatabaseService

logger = logging.getLogger(__name__)

class YouTubeDownloader:
    def __init__(self, settings: Settings, cache_service: CacheService, db_service: DatabaseService, event_bus):
        self._settings = settings
        self._cache = cache_service
        self._db = db_service
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.ytmusic = YTMusic()
        self.http_client = httpx.AsyncClient(timeout=15.0)
        
        self.fail_cooldown = {}
        self.yt_cookies_path = self._settings.WRITABLE_DIR / "youtube_cookies.txt"
        self.sc_cookies_path = self._settings.WRITABLE_DIR / "soundcloud_cookies.txt"

        if self._settings.YT_COOKIES:
            with open(self.yt_cookies_path, "w", encoding="utf-8") as f: f.write(self._settings.YT_COOKIES)
        if self._settings.SC_COOKIES:
            with open(self.sc_cookies_path, "w", encoding="utf-8") as f: f.write(self._settings.SC_COOKIES)

    async def search(self, query: str, limit: int = 10, **kwargs) -> List[TrackInfo]:
        if not query or not query.strip(): return []
        
        # 1. Попытка через YTMusicAPI
        logger.info(f"🔎 YTMusic Search: {query}")
        try:
            search_results = await asyncio.to_thread(self.ytmusic.search, query, filter="songs", limit=limit)
            results = self._parse_ytmusic_results(search_results, query)
            if results: return results
        except Exception as e:
            logger.warning(f"⚠️ YTMusic Search failed: {e}")

        # 2. Фолбэк через Piped API
        logger.info(f"🔄 Falling back to Piped API search: {query}")
        return await self._search_via_piped(query, limit)

    def _parse_ytmusic_results(self, search_results, query) -> List[TrackInfo]:
        results = []
        for item in search_results:
            video_id = item.get('videoId')
            if not video_id: continue
            
            title = item.get('title', '')
            similarity = difflib.SequenceMatcher(None, query.lower(), title.lower()).ratio()
            if similarity < 0.2: continue

            artists = ", ".join([a['name'] for a in item.get('artists', [])])
            duration_text = item.get('duration', '0:00')
            try:
                parts = duration_text.split(':')
                duration = sum(int(p) * 60**i for i, p in enumerate(reversed(parts)))
            except: duration = 0
            if not (self._settings.TRACK_MIN_DURATION_S <= duration <= self._settings.TRACK_MAX_DURATION_S): continue
            
            track = TrackInfo(identifier=video_id, title=item.get('title'), duration=duration, uploader=artists,
                              thumbnail_url=item.get('thumbnails', [{}])[-1].get('url'), source=Source.YTMUSIC)
            results.append(track)
        return results

    async def _search_via_piped(self, query: str, limit: int) -> List[TrackInfo]:
        try:
            resp = await self.http_client.get(f"https://pipedapi.kavin.rocks/search?q={query}&filter=songs")
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("items", [])[:limit]:
                track = TrackInfo(identifier=item['videoId'], title=item['title'], uploader=item.get('uploaderName', 'Unknown'),
                                  source=Source.YOUTUBE, duration=0)
                results.append(track)
            return results
        except Exception as e:
            logger.error(f"❌ Piped search failed: {e}")
            return []

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        await asyncio.sleep(random.uniform(5, 15))
        if video_id in self.fail_cooldown and (time.time() - self.fail_cooldown[video_id] < 3600):
            return DownloadResult(success=False, error_message="Cooldown")

        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"
        if final_path.exists():
            return DownloadResult(success=True, file_path=final_path, track_info=track_info)

        async with self.semaphore:
            for method in [self._download_via_soundcloud, self._download_via_youtube]:
                result = await method(track_info, final_path)
                if result.success:
                    result.track_info = track_info
                    return result
        
        self.fail_cooldown[video_id] = time.time()
        return DownloadResult(success=False, error_message="All methods failed")

    async def _download_via_youtube(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        return await self._download_with_yt_dlp(f"https://www.youtube.com/watch?v={track_info.identifier}", target_path, self.yt_cookies_path)

    async def _download_via_soundcloud(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        return await self._download_with_yt_dlp(f"scsearch1:{track_info.uploader} - {track_info.title}", target_path, self.sc_cookies_path)

    async def _download_with_yt_dlp(self, url: str, target_path: Path, cookie_path: Path) -> DownloadResult:
        temp_path = target_path.with_suffix('.mp3')
        opts = {
            'format': 'bestaudio/best', 'outtmpl': str(temp_path), 'quiet': True, 'noprogress': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'js_runtimes': 'deno'
        }
        if cookie_path.exists(): opts['cookiefile'] = str(cookie_path)
        
        try:
            await asyncio.to_thread(yt_dlp.YoutubeDL(opts).download, [url])
            return DownloadResult(success=True, file_path=target_path) if target_path.exists() else DownloadResult(success=False)
        except Exception as e:
            return DownloadResult(success=False, error_message=str(e))
