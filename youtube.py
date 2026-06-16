import asyncio
import logging
import subprocess
import random
from pathlib import Path
from typing import List, Optional

import yt_dlp
import httpx
from config import Settings
from models import DownloadResult, TrackInfo, Source
from cache_service import CacheService
from db_service import DatabaseService

logger = logging.getLogger(__name__)

# Список надежных инстансов Piped
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.moomoo.me",
    "https://piped-api.garudalinux.org"
]

class YouTubeDownloader:
    def __init__(self, settings: Settings, cache_service: CacheService, db_service: DatabaseService, event_bus):
        self._settings = settings
        self._cache = cache_service
        self._db = db_service
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.http_client = httpx.AsyncClient(timeout=10.0)

    async def search(self, query: str, limit: int = 5) -> List[TrackInfo]:
        if not query or not query.strip(): return []
        
        # 1. Попытка через сеть Piped инстансов
        for instance in random.sample(PIPED_INSTANCES, len(PIPED_INSTANCES)):
            try:
                logger.info(f"🔎 Piped Search ({instance}): {query}")
                resp = await self.http_client.get(f"{instance}/search?q={query}&filter=songs")
                resp.raise_for_status()
                data = resp.json()
                results = []
                for item in data.get("items", [])[:limit]:
                    results.append(TrackInfo(identifier=item['videoId'], title=item['title'], uploader=item.get('uploaderName', 'Unknown'), source=Source.YOUTUBE, duration=0))
                if results: return results
            except Exception as e:
                logger.warning(f"⚠️ Piped search failed on {instance}: {e}")
        
        return []

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        # Пауза для маскировки
        await asyncio.sleep(random.uniform(2, 5))
        
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"
        if final_path.exists():
            return DownloadResult(success=True, file_path=final_path, track_info=track_info)

        # Скачивание напрямую через yt-dlp (Deno)
        return await self._download_with_yt_dlp(f"https://www.youtube.com/watch?v={video_id}", final_path)

    async def _download_with_yt_dlp(self, url: str, target_path: Path) -> DownloadResult:
        opts = {
            'format': 'bestaudio/best', 'outtmpl': str(target_path), 'quiet': True, 'noprogress': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'js_runtimes': 'deno'
        }
        
        try:
            await asyncio.to_thread(yt_dlp.YoutubeDL(opts).download, [url])
            return DownloadResult(success=True, file_path=target_path) if target_path.exists() else DownloadResult(success=False)
        except Exception as e:
            return DownloadResult(success=False, error_message=str(e))
