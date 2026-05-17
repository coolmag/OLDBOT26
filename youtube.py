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
                    # ⚠️ ПРАВИЛЬНАЯ МАТЕМАТИКА ВРЕМЕНИ
                    if len(parts) == 3: # Формат ЧЧ:ММ:СС
                        duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    elif len(parts) == 2: # Формат ММ:СС
                        duration = int(parts[0]) * 60 + int(parts[1])
                    else: # Формат СС
                        duration = int(parts[0])
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
            # --- СТРАТЕГИЯ №1: Invidious API ---
            logger.info(f"▶️ [Invidious] Attempting direct download for {video_id}")
            invidious_res = await self._download_via_invidious(video_id, final_path)
            if invidious_res.success:
                invidious_res.track_info = track_info
                return invidious_res

            # --- СТРАТЕГИЯ №3 (ПОСЛЕДНИЙ ШАНС): Локальный yt-dlp ---
            logger.warning(f"🔴 [yt-dlp Fallback] Piped and Invidious failed. Falling back to local yt-dlp for {video_id}")
            yt_res = await self._download_youtube_native(video_id, final_path)
            if yt_res.success:
                yt_res.track_info = track_info
                return yt_res
                
        return DownloadResult(success=False, error_message="All download methods (Piped, Invidious, yt-dlp) failed")


    async def _download_via_invidious(self, video_id: str, target_path: Path) -> DownloadResult:
        instances = self._settings.INVIDIOUS_INSTANCES.copy()
        random.shuffle(instances)
        
        for instance in instances:
            try:
                api_url = f"{instance.rstrip('/')}/api/v1/videos/{video_id}"
                async with httpx.AsyncClient() as client:
                    logger.debug(f"▶️ [Invidious] Querying {api_url}")
                    response = await client.get(api_url, timeout=20)
                    response.raise_for_status()
                    data = response.json()
                    
                    audio_streams = [s for s in data.get('adaptiveFormats', []) if s.get('type', '').startswith('audio/mp4')]
                    if not audio_streams:
                        logger.warning(f"▶️ [Invidious] No M4A audio streams found on {instance} for {video_id}")
                        continue
                        
                    best_stream = max(audio_streams, key=lambda s: s.get('bitrate', 0))
                    stream_url = best_stream.get('url')
                    
                    if not stream_url:
                        logger.warning(f"▶️ [Invidious] Best stream has no URL on {instance}")
                        continue

                    logger.info(f"▶️ [Invidious] Streaming from {instance} (Bitrate: {best_stream.get('bitrate')})")
                    
                    async with client.stream("GET", stream_url, timeout=120) as stream_response:
                        stream_response.raise_for_status()
                        with open(target_path, "wb") as f:
                            async for chunk in stream_response.aiter_bytes():
                                f.write(chunk)
                    
                    if target_path.exists() and target_path.stat().st_size > 10000:
                        logger.success(f"✅ Success via Invidious: {video_id}")
                        return DownloadResult(success=True, file_path=target_path)
                    else:
                        logger.error(f"▶️ [Invidious] Download failed: file is too small or missing from {instance}.")

            except Exception as e:
                logger.warning(f"▶️ [Invidious] Instance {instance} failed for {video_id}: {e}")
                continue
        
        return DownloadResult(success=False, error_message="All Invidious instances failed")

    # 🟢 ДОБАВЛЕН НОВЫЙ МЕТОД:
    async def _download_youtube_native(self, video_id: str, target_path: Path) -> DownloadResult:
        temp_path = str(target_path).replace(".mp3", "_yt_temp")
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_path,
            'quiet': True,
            'noprogress': True,
            'max_filesize': 25000000, 
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
        }
        try:
            loop = asyncio.get_running_loop()
            url = f"https://www.youtube.com/watch?v={video_id}"
            await loop.run_in_executor(None, lambda: self._run_yt_dlp(opts, url))
            
            paths = [Path(temp_path + ".mp3"), Path(temp_path)]
            for p in paths:
                if p.exists() and p.stat().st_size > 10000:
                    if p != target_path:
                        if target_path.exists(): target_path.unlink(missing_ok=True)
                        p.rename(target_path)
                    logger.info(f"✅ Success via Native YouTube: {video_id}") 
                    return DownloadResult(success=True, file_path=target_path)
        except Exception as e:
            logger.error(f"Native YouTube fallback failed for {video_id}: {e}")
        return DownloadResult(success=False, error_message="YT Native failed")

    async def _get_track_info_from_ytmusic(self, video_id: str) -> Optional[TrackInfo]:
        try:
            loop = asyncio.get_running_loop()
            song_data = await loop.run_in_executor(None, lambda: self.ytmusic.get_song(video_id))
            if not song_data or not song_data.get('videoDetails'): return None
            details = song_data['videoDetails']
            track_info = TrackInfo(identifier=details['videoId'], title=details['title'], uploader=details.get('author', ''), duration=int(details.get('lengthSeconds', 0)), url=f"https://music.youtube.com/watch?v={details['videoId']}", thumbnail_url=details['thumbnail']['thumbnails'][-1]['url'] if details.get('thumbnail') else None, source=Source.YOUTUBE)
            await self._cache.set(f"trackinfo:{video_id}", dataclasses.asdict(track_info), ttl=3600 * 24 * 7)
            return track_info
        except Exception as e:
            # 🟢 Логируем ошибки парсинга, чтобы не гадать, почему треки без инфы
            logger.error(f"Error reading from YTMusic details for {video_id}: {e}")
            return None

    async def _get_track_info_from_cache(self, video_id: str) -> Optional[TrackInfo]:
        cached_info = await self._cache.get(f"trackinfo:{video_id}")
        if cached_info:
            return TrackInfo(**cached_info)
        return None

    def _run_yt_dlp(self, opts, url):
        # 🟢 Добавляем cookies, если они существуют. Это наш главный метод аутентификации.
        cookie_file_to_use = self._settings.YTDLP_COOKIES_FILE or self._settings.COOKIES_FILE

        if cookie_file_to_use and cookie_file_to_use.exists():
            opts['cookiefile'] = str(cookie_file_to_use)
            logger.info(f"Using cookiefile: {cookie_file_to_use}")
        else:
            # Если куки-файла нет, не имеет смысла даже пытаться качать с ютуба при текущих блокировках
            logger.warning("No cookie file found, YouTube download will likely fail.")


        # 🟢 КОМБИНИРОВАННАЯ СТРАТЕГИЯ: Cookies + Эмуляция клиента для обхода ошибок подписи
        final_opts = {
            **opts, 
            'retries': 5, 
            'compat_opts': ['no-live-chat', 'no-playlist-entries', 'no-xml-channel'],
            'extractor_args': {
                'youtube': {
                    'player_client': ['ios', 'tv', 'web'], 
                    'skip': ['hls', 'dash'] 
                }
            }
        }
        with yt_dlp.YoutubeDL(final_opts) as ydl:
            ydl.download([url])
