import asyncio
import logging
import dataclasses
import random
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
    🎵 Aurora Downloader Engine (v5.0 - SoundCloud First).
    Focusing on SoundCloud as the primary download source for reliability.
    """
    
    def __init__(self, settings: Settings, cache_service: CacheService):
        self._settings = settings
        self._cache = cache_service
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(1) # Снижаем до 1 одновременной загрузки во избежание бана
        self.ytmusic = YTMusic() 

    async def search(self, query: str, limit: int = 10, **kwargs) -> List[TrackInfo]:
        """По-прежнему ищет метаданные на YTMusic, т.к. это быстро и качественно."""
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
                    if len(parts) == 3: duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    elif len(parts) == 2: duration = int(parts[0]) * 60 + int(parts[1])
                    else: duration = int(parts[0])
                except: 
                    duration = 0
                
                if duration > getattr(self._settings, 'TRACK_MAX_DURATION_S', 900): 
                    continue

                track = TrackInfo(
                    identifier=video_id,
                    title=item.get('title'),
                    duration=duration,
                    uploader=artists,
                    thumbnail_url=item.get('thumbnails', [{}])[-1].get('url'),
                    source=Source.YTMUSIC
                )
                results.append(track)
            
            return results

        except Exception as e:
            logger.error(f"❌ YTMusic Search error: {e}", exc_info=True)
            return []

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        """
        Основной метод загрузки. Теперь ВСЕГДА пытается скачать с SoundCloud, используя
        метаданные, полученные из YTMusic.
        """
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
            # --- ОСНОВНАЯ СТРАТЕГИЯ: SoundCloud ---
            logger.info(f"☁️ Attempting to download '{track_info.title}' via SoundCloud...")
            sc_result = await self._download_via_soundcloud(track_info, final_path)
            if sc_result.success:
                sc_result.track_info = track_info
                return sc_result
            
            # --- РЕЗЕРВНАЯ СТРАТЕГИЯ (на случай если SoundCloud не найдет): yt-dlp с YouTube ---
            logger.warning(f"🔴 SoundCloud failed. Falling back to native yt-dlp for YouTube ID {video_id}")
            yt_res = await self._download_youtube_native(video_id, final_path)
            if yt_res.success:
                yt_res.track_info = track_info
                return yt_res
                
        return DownloadResult(success=False, error_message="All download methods (SoundCloud, yt-dlp) failed")

    async def _download_via_soundcloud(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        """Ищет и скачивает трек с SoundCloud по данным из TrackInfo."""
        if not track_info.uploader or not track_info.title:
            return DownloadResult(success=False, error_message="Not enough track info for SoundCloud search")

        search_query = f"scsearch1:{track_info.uploader} - {track_info.title}"
        temp_path = str(target_path).replace(".mp3", "_sc_temp")
        
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_path,
            'quiet': True,
            'noprogress': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            'force_ipv4': True,
            'sleep_interval': 8,
            'max_sleep_interval': 15,
            'ratelimit': 800_000,
            'retries': 3,
        }
        
        # Используем специальный файл куки для SoundCloud
        sc_cookies = self._settings.WRITABLE_DIR / "soundcloud_cookies.txt"
        if sc_cookies.exists():
            opts['cookiefile'] = str(sc_cookies)
            logger.info("Using SoundCloud cookies for download.")
        else:
            logger.warning("No soundcloud_cookies.txt found. Download will likely fail or be low quality.")

        try:
            loop = asyncio.get_running_loop()
            logger.info(f"☁️ [yt-dlp] Searching SoundCloud: {search_query}")
            await loop.run_in_executor(None, lambda: self._run_yt_dlp(opts, search_query))

            paths = [Path(temp_path + ".mp3"), Path(temp_path)]
            for p in paths:
                if p.exists() and p.stat().st_size > 10000:
                    if p != target_path:
                        if target_path.exists(): target_path.unlink(missing_ok=True)
                        p.rename(target_path)
                    logger.success(f"✅ Success via SoundCloud: {search_query}")
                    return DownloadResult(success=True, file_path=target_path)

            logger.error("☁️ SoundCloud download finished but file is missing or too small.")
            return DownloadResult(success=False, error_message="SoundCloud file not found")

        except Exception as e:
            logger.error(f"☁️ SoundCloud download failed: {e}")
            return DownloadResult(success=False, error_message=str(e))
            
    async def _download_youtube_native(self, video_id: str, target_path: Path) -> DownloadResult:
        """Резервный метод скачивания с YouTube. Менее надежен."""
        temp_path = str(target_path).replace(".mp3", "_yt_temp")
        opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'outtmpl': temp_path,
            'quiet': True,
            'noprogress': True,
            'max_filesize': 25_000_000,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            'force_ipv4': True,
            'js_runtimes': {'node': {}},
        }
        
        # Для YouTube используем основной файл куки
        yt_cookies = self._settings.YTDLP_COOKIES_FILE or self._settings.COOKIES_FILE
        if yt_cookies and yt_cookies.exists():
             opts['cookiefile'] = str(yt_cookies)
             logger.info(f"Using YouTube cookies: {yt_cookies}")
        else:
            logger.warning("No YouTube cookies found, download will likely fail.")
            
        try:
            loop = asyncio.get_running_loop()
            url = f"https://www.youtube.com/watch?v={video_id}"
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: self._run_yt_dlp(opts, url)),
                timeout=60.0
            )
            
            paths = [Path(temp_path + ".mp3"), Path(temp_path)]
            for p in paths:
                if p.exists() and p.stat().st_size > 10000:
                    if p != target_path:
                        if target_path.exists(): target_path.unlink(missing_ok=True)
                        p.rename(target_path)
                    logger.info(f"✅ Success via Native YouTube: {video_id}") 
                    return DownloadResult(success=True, file_path=target_path)
        
        except asyncio.TimeoutError:
            logger.error(f"Native YouTube fallback timed out for {video_id}")
            return DownloadResult(success=False, error_message="Native YouTube download timed out")

        except Exception as e:
            logger.error(f"Native YouTube fallback failed for {video_id}: {e}")
            return DownloadResult(success=False, error_message="YT Native failed")

    def _run_yt_dlp(self, opts, url):
        """Универсальный метод для запуска yt-dlp в executor'е."""
        
        final_opts = {**opts}
        # Применяем extractor_args только для YouTube
        if "youtube.com" in url or "youtu.be" in url:
            final_opts['extractor_args'] = {
                'youtube': { 'player_client': ['web', 'tv'], 'skip': ['hls', 'dash'] }
            }
        
        with yt_dlp.YoutubeDL(final_opts) as ydl:
            ydl.download([url])

    async def _get_track_info_from_ytmusic(self, video_id: str) -> Optional[TrackInfo]:
        try:
            loop = asyncio.get_running_loop()
            song_data = await loop.run_in_executor(None, lambda: self.ytmusic.get_song(video_id))
            if not song_data or not song_data.get('videoDetails'): return None
            details = song_data['videoDetails']
            track_info = TrackInfo(identifier=details['videoId'], title=details['title'], uploader=details.get('author', ''), duration=int(details.get('lengthSeconds', 0)), url=f"https://music.youtube.com/watch?v={details['videoId']}", thumbnail_url=details['thumbnail']['thumbnails'][-1]['url'] if details.get('thumbnail') else None, source=Source.YTMUSIC)
            await self._cache.set(f"trackinfo:{video_id}", dataclasses.asdict(track_info), ttl=3600 * 24 * 7)
            return track_info
        except Exception as e:
            logger.error(f"Error reading from YTMusic details for {video_id}: {e}")
            return None

    async def _get_track_info_from_cache(self, video_id: str) -> Optional[TrackInfo]:
        cached_info = await self._cache.get(f"trackinfo:{video_id}")
        if cached_info:
            return TrackInfo(**cached_info)
        return None
