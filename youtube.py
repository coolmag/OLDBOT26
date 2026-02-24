import asyncio
import logging
from pathlib import Path
from typing import List, Optional

import yt_dlp
from ytmusicapi import YTMusic

from config import Settings
from models import DownloadResult, TrackInfo, Source
from cache_service import CacheService
from cobalt_downloader import CobaltDownloader

logger = logging.getLogger(__name__)

class YouTubeDownloader:
    """
    Hybrid Downloader (Cobalt -> yt-dlp -> SoundCloud)
    """

    def __init__(self, settings: Settings, cache_service: CacheService, cobalt_downloader: CobaltDownloader):
        self._settings = settings
        self._cache = cache_service
        self._cobalt = cobalt_downloader
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(2)  # Allow 2 parallel local downloads
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
                    if not (0 < duration < self._settings.MAX_TRACK_DURATION_S):
                        continue
                        
                    artist_str = ", ".join([a['name'] for a in item.get('artists', [])])
                    track = TrackInfo(
                        identifier=item['videoId'], # Use identifier
                        title=item['title'],
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
            logger.error(f"Error searching YTMusic: {e}", exc_info=True)
            return []

    async def get_track_info_by_id(self, video_id: str) -> Optional[TrackInfo]:
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
                identifier=details['videoId'],
                title=details['title'],
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
        # If we only have the ID, get the full track info first
        if not track_info:
            track_info = await self.get_track_info_by_id(video_id)
            if not track_info:
                return DownloadResult(success=False, error_message=f"Could not get track info for {video_id}")

        # Priority 1: Cobalt API
        logger.info(f"[{video_id}] Trying Cobalt downloader...")
        cobalt_result = await self._cobalt.download(video_id)
        if cobalt_result and cobalt_result.success:
            cobalt_result.track_info = track_info # Add track info to the result
            return cobalt_result

        logger.warning(f"[{video_id}] Cobalt failed. Falling back to local download.")
        
        # If we reach here, Cobalt failed. We proceed with local download methods.
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"
        if final_path.exists() and final_path.stat().st_size > 10000:
            logger.info(f"✅ Cache hit for local file {video_id}")
            return DownloadResult(success=True, file_path=final_path, track_info=track_info, is_url=False)

        # Priority 2: yt-dlp (direct download, no proxy)
        async with self.semaphore:
            logger.info(f"[{video_id}] Trying direct yt-dlp download...")
            res = await self._download_yt_dlp(video_id, final_path, track_info)
            if res.success:
                return res

        # Priority 3: SoundCloud Fallback
        artist = track_info.uploader
        sc_query = f"{artist} - {track_info.title}"
        logger.info(f"[{video_id}] ☁️ Falling back to SoundCloud with query: '{sc_query}'")
        return await self._download_soundcloud_fallback(sc_query, final_path, track_info)

    async def _download_yt_dlp(self, video_id: str, target_path: Path, track_info: TrackInfo) -> DownloadResult:
        temp_path_str = str(target_path).replace(".mp3", "_temp")
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_path_str,
            'quiet': True,
            'nocheckcertificate': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
        }

        if self._settings.COOKIES_FILE.exists():
            opts['cookiefile'] = str(self._settings.COOKIES_FILE)
            logger.info(f"[{video_id}] Using cookies file for yt-dlp.")

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._run_yt_dlp_exec(opts, f"https://music.youtube.com/watch?v={video_id}"))

            final_temp_path = self._find_temp_file(temp_path_str)
            if final_temp_path:
                if target_path.exists(): target_path.unlink()
                final_temp_path.rename(target_path)
                logger.success(f"✅ Direct yt-dlp download successful for {video_id}")
                return DownloadResult(success=True, file_path=target_path, track_info=track_info, is_url=False)
        except Exception as e:
            logger.warning(f"❌ Direct yt-dlp download failed for {video_id}: {e}")

        # Cleanup failed attempts
        self._cleanup_temp_file(temp_path_str)
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
            await loop.run_in_executor(None, lambda: self._run_yt_dlp_exec(opts, query))

            final_temp_path = self._find_temp_file(temp_path_str)
            if final_temp_path:
                if target_path.exists(): target_path.unlink()
                final_temp_path.rename(target_path)
                logger.success(f"✅ SoundCloud fallback successful for: {query}")
                updated_track_info = track_info.copy(update={'source': Source.SOUNDCLOUD})
                return DownloadResult(success=True, file_path=target_path, track_info=updated_track_info, is_url=False)
        except Exception as e:
            logger.error(f"❌ SoundCloud fallback failed: {e}")
        
        self._cleanup_temp_file(temp_path_str)
        return DownloadResult(success=False, error_message="SoundCloud download failed")

    def _run_yt_dlp_exec(self, opts: dict, url: str):
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

    def _find_temp_file(self, temp_base: str) -> Optional[Path]:
        """Finds the output file from yt-dlp, which might have a different extension."""
        temp_path_processed = Path(temp_base + ".mp3")
        if temp_path_processed.exists() and temp_path_processed.stat().st_size > 10000:
            return temp_path_processed
        
        temp_path_original = Path(temp_base)
        if temp_path_original.exists() and temp_path_original.stat().st_size > 10000:
            return temp_path_original
        
        return None

    def _cleanup_temp_file(self, temp_base: str):
        """Removes temporary files from a failed download."""
        for p in [Path(temp_base + ".mp3"), Path(temp_base)]:
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
