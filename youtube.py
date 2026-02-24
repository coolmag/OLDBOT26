import asyncio
import logging
import inspect
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
    🎵 Aurora Downloader Engine (2026 Edition).
    Waterfall Strategy: Cobalt API -> yt-dlp (cookies) -> SoundCloud.
    """
    
    def __init__(self, settings: Settings, cache_service: CacheService):
        self._settings = settings
        self._cache = cache_service
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(2) # Максимум 2 одновременных загрузки
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
            logger.error(f"❌ YTMusic Search error: {e}")
            return []

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"
        
        if final_path.exists() and final_path.stat().st_size > 10000:
            logger.info(f"✅ Cache hit for {video_id}")
            # Even if cached, ensure we return track_info if it was passed or can be fetched
            if not track_info:
                track_info = await self._get_track_info_from_cache(video_id)
            return DownloadResult(success=True, file_path=final_path, track_info=track_info)
            
        if not track_info:
            track_info = await self._get_track_info_from_cache(video_id)
            if not track_info:
                 return DownloadResult(success=False, error_message=f"Could not get track info for {video_id}")

        async with self.semaphore:
            # 1. СТРАТЕГИЯ: COBALT API
            logger.info(f"🎧 [Cobalt] Attempting download for {video_id}...")
            cobalt_res = await self._download_cobalt(video_id, final_path)
            if cobalt_res.success:
                cobalt_res.track_info = track_info
                return cobalt_res
                
            # 2. ФОЛБЭК 1: Локальный yt-dlp с куками
            logger.warning(f"⚠️ [yt-dlp] Cobalt failed, falling back to local yt-dlp...")
            yt_res = await self._download_yt_local(video_id, final_path)
            if yt_res.success:
                yt_res.track_info = track_info
                return yt_res

            # 3. ФОЛБЭК 2: SoundCloud
            artist = getattr(track_info, 'uploader', getattr(track_info, 'artist', ''))
            sc_query = f"{artist} - {track_info.title}"
            logger.warning(f"☁️ [SoundCloud] YouTube blocked. Searching '{sc_query}'...")
            sc_res = await self._download_soundcloud_fallback(sc_query, final_path)
            if sc_res.success:
                sc_res.track_info = track_info
                return sc_res
                
        return DownloadResult(success=False, error_message="All download providers failed")

    async def _download_cobalt(self, video_id: str, target_path: Path) -> DownloadResult:
        yt_url = f"https://music.youtube.com/watch?v={video_id}"
        
        payload = {
            "url": yt_url,
            "downloadMode": "audio", "audioFormat": "mp3",
            "isAudioOnly": True, "aFormat": "mp3"
        }
        headers = {
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
        }

        instances = self._settings.COBALT_INSTANCES or ["https://api.cobalt.tools"]
        
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            for instance in instances:
                base_url = instance.rstrip('/')
                for endpoint in [f"{base_url}/api/json", f"{base_url}/"]:
                    try:
                        resp = await client.post(endpoint, json=payload, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            audio_url = data.get("url")
                            if data.get("status") == "picker" and data.get("picker"):
                                audio_url = data["picker"][0].get("url")
                                
                            if audio_url:
                                async with client.stream("GET", audio_url) as stream_resp:
                                    stream_resp.raise_for_status()
                                    with open(target_path, "wb") as f:
                                        async for chunk in stream_resp.aiter_bytes():
                                            f.write(chunk)
                                logger.info(f"✅ Success via Cobalt ({instance})")
                                return DownloadResult(success=True, file_path=target_path)
                    except Exception:
                        continue
                        
        return DownloadResult(success=False, error_message="Cobalt instances unavailable")

    async def _download_yt_local(self, video_id: str, target_path: Path) -> DownloadResult:
        temp_path = str(target_path).replace(".mp3", "_temp")
        opts = {
            'format': 'bestaudio/best', 'outtmpl': temp_path, 'quiet': True, 
            'noprogress': True, 'nocheckcertificate': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3'}]
        }
        
        if self._settings.COOKIES_FILE.exists():
            opts['cookiefile'] = str(self._settings.COOKIES_FILE)
            
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._run_yt_dlp(opts, f"https://music.youtube.com/watch?v={video_id}"))
            
            paths = [Path(temp_path + ".mp3"), Path(temp_path)]
            for p in paths:
                if p.exists() and p.stat().st_size > 10000:
                    if p != target_path: 
                        if target_path.exists(): target_path.unlink(missing_ok=True)
                        p.rename(target_path)
                    logger.info(f"✅ Success via local yt-dlp")
                    return DownloadResult(success=True, file_path=target_path)
        except Exception as e:
            logger.warning(f"Local yt-dlp failed: {e}")

        return DownloadResult(success=False)

    async def _download_soundcloud_fallback(self, query: str, target_path: Path) -> DownloadResult:
        temp_path = str(target_path).replace(".mp3", "_sc_temp")
        opts = {
            'format': 'bestaudio/best', 'outtmpl': temp_path, 'quiet': True, 
            'noprogress': True, 'noplaylist': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio','preferredcodec': 'mp3','preferredquality': '192'}]
        }
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

    async def _get_track_info_from_cache(self, video_id: str) -> Optional[TrackInfo]:
        """Helper to get track info only from cache."""
        cached_info = await self._cache.get(f"trackinfo:{video_id}")
        if cached_info:
            return TrackInfo(**cached_info)
        return None

    def _run_yt_dlp(self, opts, url):
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
