import asyncio
import logging
from pathlib import Path
from typing import List, Optional

import yt_dlp
from ytmusicapi import YTMusic

from config import Settings
from models import DownloadResult, TrackInfo, Source
from cache_service import CacheService
from proxy_service import ProxyManager

logger = logging.getLogger(__name__)

class YouTubeDownloader:
    def __init__(self, settings: Settings, cache_service: CacheService, proxy_manager: ProxyManager):
        self._settings = settings
        self._cache = cache_service
        self._proxy_manager = proxy_manager
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(2)  # Allow 2 parallel downloads
        self.ytmusic = YTMusic()

    async def search(self, query: str, limit: int = 10) -> List[TrackInfo]:
        logger.info(f"Searching on YouTube Music for: {query}")
        try:
            loop = asyncio.get_running_loop()
            search_results = await loop.run_in_executor(
                None, lambda: self.ytmusic.search(query, filter="songs", limit=limit)
            )

            tracks = []
            for item in search_results:
                if item.get('videoId'):
                    duration = item.get('duration_seconds', 0)
                    if duration > self._settings.MAX_TRACK_DURATION_S:
                        continue
                        
                    artist_str = ", ".join([a['name'] for a in item.get('artists', [])])
                    track = TrackInfo(
                        id=item['videoId'],
                        title=item['title'],
                        artist=artist_str,
                        uploader=artist_str, # For consistency
                        duration=duration,
                        url=f"https://music.youtube.com/watch?v={item['videoId']}",
                        thumbnail_url=item['thumbnails'][-1]['url'] if item.get('thumbnails') else None,
                        source=Source.YOUTUBE,
                    )
                    tracks.append(track)
            logger.info(f"✅ Found {len(tracks)} tracks on YTMusic")
            return tracks
        except Exception as e:
            logger.error(f"Error searching YTMusic: {e}")
            return []

    async def get_track_info_by_id(self, video_id: str) -> Optional[TrackInfo]:
        """Gets track metadata by its ID from YouTube Music, using a cache."""
        cache_key = f"trackinfo:{video_id}"
        cached_info = await self._cache.get(cache_key)
        if cached_info:
            return TrackInfo(**cached_info)

        logger.info(f"Fetching metadata for video_id: {video_id} from API")
        try:
            loop = asyncio.get_running_loop()
            song_data = await loop.run_in_executor(None, lambda: self.ytmusic.get_song(video_id))
            
            if not song_data or not song_data.get('videoDetails'):
                logger.warning(f"No metadata found for {video_id}")
                return None

            details = song_data['videoDetails']
            artist_str = details.get('author', '')
            track_info = TrackInfo(
                id=details['videoId'],
                title=details['title'],
                artist=artist_str,
                uploader=artist_str,
                duration=int(details.get('lengthSeconds', 0)),
                url=f"https://music.youtube.com/watch?v={details['videoId']}",
                thumbnail_url=details['thumbnail']['thumbnails'][-1]['url'] if details.get('thumbnail') else None,
                source=Source.YOUTUBE,
            )
            await self._cache.set(cache_key, track_info.dict(), ttl=3600 * 24 * 7) # Cache for 1 week
            return track_info
        except Exception as e:
            logger.error(f"Failed to get track info for {video_id}: {e}", exc_info=True)
            return None

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"
        if final_path.exists() and final_path.stat().st_size > 10000:
            logger.info(f"✅ Cache hit for {video_id}")
            if not track_info:
                track_info = await self.get_track_info_by_id(video_id)
            return DownloadResult(success=True, file_path=final_path, track_info=track_info)

        if not track_info:
            track_info = await self.get_track_info_by_id(video_id)
            if not track_info:
                return DownloadResult(success=False, error_message=f"Could not get track info for {video_id}")

        # 1. Attempt yt-dlp with active proxy and COOKIES
        async with self.semaphore:
            res = await self._download_yt_smart(video_id, final_path, track_info)
            if res.success:
                return res

        # 2. Fallback to SoundCloud
        artist = getattr(track_info, 'uploader', getattr(track_info, 'artist', ''))
        sc_query = f"{artist} - {track_info.title}"
        logger.info(f"☁️ Fallback: Downloading '{sc_query}' from SoundCloud...")
        return await self._download_soundcloud_fallback(sc_query, final_path, track_info)

    async def _download_yt_smart(self, video_id: str, target_path: Path, track_info: Optional[TrackInfo]) -> DownloadResult:
        temp_path_str = str(target_path).replace(".mp3", "_temp")
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_path_str,
            'quiet': True,
            'nocheckcertificate': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
            'extractor_args': {'youtube': {'player_client': ['android']}},
        }

        if self._settings.COOKIES_FILE.exists():
            opts['cookiefile'] = str(self._settings.COOKIES_FILE)
            logger.info("🍪 Using cookies file for yt-dlp.")

        proxy_url = self._proxy_manager.active_proxy_url
        if proxy_url:
            opts['proxy'] = proxy_url
            logger.info(f"🔩 Using proxy for yt-dlp: {proxy_url}")
        else:
            logger.warning("🔹 No active proxy for yt-dlp download.")

        try:
            loop = asyncio.get_running_loop()
            logger.info(f"🎧 Downloading {video_id} via yt-dlp...")
            await loop.run_in_executor(None, lambda: self._run_yt_dlp(opts, f"https://music.youtube.com/watch?v={video_id}"))

            temp_path_processed = Path(temp_path_str + ".mp3")
            temp_path_original = Path(temp_path_str)

            final_temp_path = None
            if temp_path_processed.exists() and temp_path_processed.stat().st_size > 10000:
                final_temp_path = temp_path_processed
            elif temp_path_original.exists() and temp_path_original.stat().st_size > 10000:
                final_temp_path = temp_path_original

            if final_temp_path:
                if target_path.exists(): target_path.unlink()
                final_temp_path.rename(target_path)
                logger.success(f"✅ YT Download successful for {video_id}")
                return DownloadResult(success=True, file_path=target_path, track_info=track_info)
        except Exception as e:
            logger.warning(f"❌ YT Download failed for {video_id}: {e}")

        for p in [Path(temp_path_str + ".mp3"), Path(temp_path_str)]:
            if p.exists(): p.unlink(missing_ok=True)
        return DownloadResult(success=False)

    async def _download_soundcloud_fallback(self, query: str, target_path: Path, track_info: TrackInfo) -> DownloadResult:
        temp_path_str = str(target_path).replace(".mp3", "_sc_temp")
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_path_str,
            'quiet': True,
            'nocheckcertificate': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
            'default_search': 'scsearch1:',
        }

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._run_yt_dlp(opts, query))

            temp_path_processed = Path(temp_path_str + ".mp3")
            temp_path_original = Path(temp_path_str)

            final_temp_path = None
            if temp_path_processed.exists() and temp_path_processed.stat().st_size > 10000:
                final_temp_path = temp_path_processed
            elif temp_path_original.exists() and temp_path_original.stat().st_size > 10000:
                final_temp_path = temp_path_original

            if final_temp_path:
                if target_path.exists(): target_path.unlink()
                final_temp_path.rename(target_path)
                logger.success(f"✅ Success via SoundCloud: {query}")
                updated_track_info = track_info.copy(update={'source': Source.SOUNDCLOUD})
                return DownloadResult(success=True, file_path=target_path, track_info=updated_track_info)
        except Exception as e:
            logger.error(f"❌ SoundCloud fallback failed: {e}")

        for p in [Path(temp_path_str + ".mp3"), Path(temp_path_str)]:
            if p.exists(): p.unlink(missing_ok=True)
        return DownloadResult(success=False, error_message="SC Fallback failed")

    def _run_yt_dlp(self, opts: dict, url: str):
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
