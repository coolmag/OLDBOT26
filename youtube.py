import asyncio
import logging
import dataclasses
from pathlib import Path
from typing import List, Optional

import httpx
import yt_dlp
from ytmusicapi import YTMusic
from config import Settings
from models import DownloadResult, TrackInfo, Source
from cache_service import CacheService

logger = logging.getLogger(__name__)

class YouTubeDownloader:
    """
    🎵 Aurora Downloader Engine (v4.0 - SoundCloud Direct).
    Temporarily using SoundCloud-only for max speed while public services are down.
    """
    
    def __init__(self, settings: Settings, cache_service: CacheService):
        self._settings = settings
        self._cache = cache_service
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(2)
        self.ytmusic = YTMusic() 

    async def search(self, query: str, limit: int = 10, **kwargs) -> List[TrackInfo]:
        if kwargs.get('decade'):
            query = f"{query} {kwargs['decade']}"
        if not query or not query.strip(): return []
            
        logger.info(f"🔎 YTMusic Search: {query}")
        
        loop = asyncio.get_running_loop()
        try:
            search_results = await loop.run_in_executor(None, lambda: self.ytmusic.search(query, filter="songs", limit=limit))
            
            results = []
            for item in search_results:
                video_id = item.get('videoId')
                if not video_id: continue
                
                artists = ", ".join([a['name'] for a in item.get('artists', [])])
                duration_text = item.get('duration', '0:00')
                try:
                    parts = duration_text.split(':')
                    duration = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0])
                except: duration = 0
                
                if duration > getattr(self._settings, 'TRACK_MAX_DURATION_S', 900): 
                    continue

                track = TrackInfo(
                    identifier=video_id,
                    title=item.get('title'),
                    duration=duration,
                    uploader=artists,
                    thumbnail_url=item.get('thumbnails', [{}])[-1].get('url'),
                    source="ytmusic"
                )
                results.append(track)
            
            return results

        except Exception as e:
            logger.error(f"❌ YTMusic Search error: {e}", exc_info=True)
            return []

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"
        
        if final_path.exists() and final_path.stat().st_size > 10000:
            logger.info(f"✅ Cache hit for {video_id}")
            if not track_info:
                track_info = await self._get_track_info_from_cache(video_id)
            return DownloadResult(success=True, file_path=final_path, track_info=track_info)
            
        if not track_info:
            track_info = await self._get_track_info_from_cache(video_id)
            if not track_info:
                 track_info = await self._get_track_info_from_ytmusic(video_id)
                 if not track_info:
                    return DownloadResult(success=False, error_message=f"Could not get track info for {video_id}")

        async with self.semaphore:
            # ⚡️ ПРЯМОЙ ФОЛЛБЭК НА SOUNDCLOUD (Без ожиданий)
            artist = getattr(track_info, 'uploader', getattr(track_info, 'artist', ''))
            sc_query = f"{artist} - {track_info.title}"
            logger.info(f"☁️ [SoundCloud] Fast fallback. Searching '{sc_query}'...")
            
            sc_res = await self._download_soundcloud_fallback(sc_query, final_path)
            if sc_res.success:
                sc_res.track_info = track_info
                return sc_res
                
        return DownloadResult(success=False, error_message="SoundCloud download failed")

    async def _download_soundcloud_fallback(self, query: str, target_path: Path) -> DownloadResult:
        temp_path = str(target_path).replace(".mp3", "_sc_temp")
        opts = {'format': 'bestaudio/best', 'outtmpl': temp_path, 'quiet': True, 'noprogress': True, 'noplaylist': True, 'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}]}
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._run_yt_dlp(opts, f"scsearch1:{query}"))
            
            paths = [Path(temp_path + ".mp3"), Path(temp_path)]
            for p in paths:
                if p.exists() and p.stat().st_size > 10000:
                    if p != target_path:
                        if target_path.exists(): target_path.unlink(missing_ok=True)
                        p.rename(target_path)
                    logger.info(f"✅ Success via SoundCloud: {query}") 
                    return DownloadResult(success=True, file_path=target_path)
        except Exception as e:
            logger.error(f"SoundCloud fallback failed: {e}")
            
        return DownloadResult(success=False, error_message="SC Fallback failed")

    async def _get_track_info_from_ytmusic(self, video_id: str) -> Optional[TrackInfo]:
        try:
            loop = asyncio.get_running_loop()
            song_data = await loop.run_in_executor(None, lambda: self.ytmusic.get_song(video_id))
            if not song_data or not song_data.get('videoDetails'): return None
            details = song_data['videoDetails']
            track_info = TrackInfo(identifier=details['videoId'], title=details['title'], uploader=details.get('author', ''), duration=int(details.get('lengthSeconds', 0)), url=f"https://music.youtube.com/watch?v={details['videoId']}", thumbnail_url=details['thumbnail']['thumbnails'][-1]['url'] if details.get('thumbnail') else None, source=Source.YOUTUBE)
            await self._cache.set(f"trackinfo:{video_id}", dataclasses.asdict(track_info), ttl=3600 * 24 * 7)
            return track_info
        except Exception:
            return None

    async def _get_track_info_from_cache(self, video_id: str) -> Optional[TrackInfo]:
        cached_info = await self._cache.get(f"trackinfo:{video_id}")
        if cached_info:
            return TrackInfo(**cached_info)
        return None

    def _run_yt_dlp(self, opts, url):
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
