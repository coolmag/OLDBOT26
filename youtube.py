import asyncio
import logging
import yt_dlp
from pathlib import Path
from typing import List, Optional
from ytmusicapi import YTMusic
from models import DownloadResult, TrackInfo, Source

logger = logging.getLogger(__name__)

class YouTubeDownloader:
    def __init__(self, settings, cache, db, event_bus):
        self._settings = settings
        self._db = db
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.ytmusic = YTMusic()
        self.yt_cookies_path = self._settings.WRITABLE_DIR / "youtube_cookies.txt"

    async def search(self, query: str, limit: int = 5) -> List[TrackInfo]:
        try:
            results = await asyncio.to_thread(self.ytmusic.search, query, filter="songs", limit=limit)
            return [TrackInfo(identifier=i['videoId'], title=i['title'], uploader=", ".join([a['name'] for a in i.get('artists', [])]), source=Source.YTMUSIC, duration=0) for i in results if i.get('videoId')]
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"
        if final_path.exists(): return DownloadResult(success=True, file_path=final_path, track_info=track_info)

        opts = {
            'format': 'bestaudio/best', 'outtmpl': str(final_path.with_suffix('.part')), 'quiet': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        if self.yt_cookies_path.exists(): opts['cookiefile'] = str(self.yt_cookies_path)
        
        try:
            await asyncio.to_thread(yt_dlp.YoutubeDL(opts).download, [f"https://www.youtube.com/watch?v={video_id}"])
            if final_path.exists(): return DownloadResult(success=True, file_path=final_path)
            return DownloadResult(success=False)
        except Exception as e:
            return DownloadResult(success=False, error_message=str(e))
