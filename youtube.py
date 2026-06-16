import asyncio
import logging
import subprocess
from pathlib import Path
from typing import List, Optional

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
        
        self.yt_cookies_path = Path("/app/youtube_cookies.txt")
        self.sc_cookies_path = Path("/app/soundcloud_cookies.txt")

    async def search(self, query: str, limit: int = 10, **kwargs) -> List[TrackInfo]:
        if not query or not query.strip(): return []
        try:
            search_results = await asyncio.to_thread(self.ytmusic.search, query, filter="songs", limit=limit)
            results = []
            for item in search_results:
                video_id = item.get('videoId')
                if not video_id: continue
                artists = ", ".join([a['name'] for a in item.get('artists', [])])
                track = TrackInfo(identifier=video_id, title=item.get('title'), duration=0, uploader=artists,
                                  source=Source.YTMUSIC)
                results.append(track)
            return results
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"
        if final_path.exists():
            return DownloadResult(success=True, file_path=final_path, track_info=track_info)

        # Прямое скачивание без пре-проверок, которые триггерят DRM
        return await self._download_with_yt_dlp(f"https://www.youtube.com/watch?v={video_id}", final_path, self.yt_cookies_path)

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
            if target_path.exists():
                return DownloadResult(success=True, file_path=target_path)
            return DownloadResult(success=False, error_message="Download failed")
        except Exception as e:
            return DownloadResult(success=False, error_message=str(e))
