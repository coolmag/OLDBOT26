import asyncio
import logging
import dataclasses
import random
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
import yt_dlp
from ytmusicapi import YTMusic

from config import Settings
from models import DownloadResult, TrackInfo, Source
from cache_service import CacheService
from jamendo import JamendoClient

logger = logging.getLogger(__name__)

class YouTubeDownloader:
    """
    🎵 Aurora Downloader Engine (v6.4 - Jamendo & FFprobe Validation).
    This version introduces Jamendo as a primary source and adds robust ffprobe
    validation for all downloads to ensure track quality and filter previews.

    Download pipeline:
    1. Audius (direct HTTP download)
    2. Jamendo (direct HTTP download)
    3. SoundCloud (yt-dlp search, now with ffprobe validation)
    """

    def __init__(self, settings: Settings, cache_service: CacheService):
        self._settings = settings
        self._cache = cache_service
        self.jamendo = JamendoClient(settings.JAMENDO_CLIENT_ID)
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(1)
        self.ytmusic = YTMusic()
        self.http_client = httpx.AsyncClient(timeout=20.0)

    async def search(self, query: str, limit: int = 10, **kwargs) -> List[TrackInfo]:
        """Metadata search via YTMusic, with min/max duration filtering."""
        # ... (unchanged from previous version)
        if kwargs.get('decade'): query = f"{query} {kwargs['decade']}"
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
                    duration = sum(int(p) * 60**i for i, p in enumerate(reversed(parts)))
                except: duration = 0
                if not (self._settings.TRACK_MIN_DURATION_S <= duration <= self._settings.TRACK_MAX_DURATION_S): continue
                track = TrackInfo(identifier=video_id, title=item.get('title'), duration=duration, uploader=artists,
                                  thumbnail_url=item.get('thumbnails', [{}])[-1].get('url'), source=Source.YTMUSIC)
                results.append(track)
            return results
        except Exception as e:
            logger.error(f"❌ YTMusic Search error: {e}", exc_info=True)
            return []

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        """Main download orchestrator following the new refined pipeline."""
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"

        if final_path.exists() and final_path.stat().st_size > 10000:
            logger.info(f"✅ Cache hit for {video_id}")
            if not track_info: track_info = await self._get_track_info_from_cache(video_id)
            return DownloadResult(success=True, file_path=final_path, track_info=track_info)

        if not track_info:
            track_info = await self._get_track_info_from_cache(video_id) or await self._get_track_info_from_ytmusic(video_id)
            if not track_info: return DownloadResult(success=False, error_message=f"Could not get track info for {video_id}")

        async with self.semaphore:
            methods = [
                self._download_via_audius,
                self._download_via_jamendo,
                self._download_via_soundcloud
            ]
            for method in methods:
                result = await method(track_info, final_path)
                if result.success:
                    result.track_info = track_info
                    return result
        
        return DownloadResult(success=False, error_message="All download methods failed")

    def _validate_audio(self, path: Path) -> Tuple[bool, str]:
        """Uses ffprobe to validate audio duration and bitrate."""
        min_dur = getattr(self._settings, 'TRACK_MIN_DURATION_S', 60)
        try:
            # Check duration
            duration_str = subprocess.check_output(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                timeout=10
            ).strip().decode('utf-8')
            duration = float(duration_str)
            if duration < min_dur:
                return False, f"Short duration ({duration:.1f}s < {min_dur}s)"

            # Check bitrate
            bitrate_str = subprocess.check_output(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=bit_rate', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                timeout=10
            ).strip().decode('utf-8')
            if bitrate_str:
                bitrate_kbps = float(bitrate_str) / 1000
                if bitrate_kbps < 128:
                    return False, f"Low bitrate ({bitrate_kbps:.0f}kbps)"
            
            return True, ""
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, FileNotFoundError) as e:
            return False, f"ffprobe validation failed: {e}"
        except Exception as e:
            return False, f"Unexpected validation error: {e}"

    async def _download_direct_http(self, audio_url: str, target_path: Path, source_name: str) -> DownloadResult:
        """Downloads a direct audio URL using httpx and validates it with ffprobe."""
        temp_path = target_path.with_suffix('.part')
        try:
            async with self.http_client.stream("GET", audio_url, follow_redirects=True) as response:
                response.raise_for_status()
                with open(temp_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)
            
            valid, msg = await asyncio.to_thread(self._validate_audio, temp_path)
            if not valid:
                temp_path.unlink(missing_ok=True)
                logger.warning(f"⚠️ [{source_name}] Rejected after download: {msg}")
                return DownloadResult(success=False, error_message=f"Quality check failed: {msg}")

            temp_path.rename(target_path)
            logger.info(f"✅ Success via {source_name} (Direct HTTP)!")
            return DownloadResult(success=True, file_path=target_path)
        except Exception as e:
            logger.error(f"❌ [{source_name}] Direct HTTP download failed: {e}")
            if temp_path.exists(): temp_path.unlink(missing_ok=True)
            return DownloadResult(success=False, error_message=str(e))

    async def _download_via_audius(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting download via Audius...")
        query = f"{track_info.uploader} {track_info.title}".strip()
        try:
            resp = await self.http_client.get(f"https://api.audius.co/v1/tracks/search?query={query}")
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if not data: 
                logger.info("Audius: No results found.")
                return DownloadResult(success=False)

            best_match = min(data, key=lambda x: abs(x.get('duration', 0) - track_info.duration), default=None)
            if best_match and abs(best_match.get('duration', 0) - track_info.duration) <= 30:
                track_id = best_match['id']
                logger.info(f"🔗 Found Audius track ID: {track_id}. Fetching stream URL...")
                stream_resp = await self.http_client.get(f"https://api.audius.co/v1/tracks/{track_id}/stream")
                stream_resp.raise_for_status()
                # Audius redirects to the actual audio URL
                audio_url = str(stream_resp.url)
                return await self._download_direct_http(audio_url, target_path, "Audius")
        except Exception as e:
            logger.error(f"Audius download pipeline failed: {e}")
        return DownloadResult(success=False)

    async def _download_via_jamendo(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting download via Jamendo...")
        audio_url = await self.jamendo.search_track_url(track_info.uploader, track_info.title)
        if audio_url:
            return await self._download_direct_http(audio_url, target_path, "Jamendo")
        return DownloadResult(success=False)

    async def _download_with_yt_dlp(self, url_or_query: str, target_path: Path, source_name: str) -> DownloadResult:
        """(SoundCloud Only) Downloads and validates using yt-dlp."""
        temp_path_str = str(target_path).replace(".mp3", f"_{source_name}_temp")
        temp_path = Path(temp_path_str)
        
        opts = {'format': 'bestaudio/best', 'outtmpl': temp_path_str, 'quiet': True, 'noprogress': True,
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
                'force_ipv4': True, 'sleep_interval': 3, 'max_sleep_interval': 10, 'retries': 2}
        sc_cookies = self._settings.WRITABLE_DIR / "soundcloud_cookies.txt"
        if sc_cookies.exists(): opts['cookiefile'] = str(sc_cookies)
        
        try:
            logger.info(f"⬇️ [{source_name}] Attempting search and download for: '{url_or_query}'")
            await asyncio.to_thread(yt_dlp.YoutubeDL(opts).download, [url_or_query])
            
            final_temp_path = temp_path.with_suffix('.mp3')
            if not final_temp_path.exists(): final_temp_path = temp_path
            if not final_temp_path.exists(): raise FileNotFoundError("yt-dlp did not produce an output file.")

            valid, msg = await asyncio.to_thread(self._validate_audio, final_temp_path)
            if not valid:
                logger.warning(f"⚠️ [{source_name}] Rejected after download: {msg}")
                final_temp_path.unlink(missing_ok=True)
                return DownloadResult(success=False, error_message=f"Quality check failed: {msg}")

            final_temp_path.rename(target_path)
            logger.info(f"✅ Success via {source_name}!")
            return DownloadResult(success=True, file_path=target_path)
        except Exception as e:
            if "DRM protected" in str(e): logger.warning(f"🛡️ [{source_name}] Failed due to DRM protection.")
            else: logger.error(f"❌ [{source_name}] Download failed: {e}")
            return DownloadResult(success=False, error_message=str(e))

    async def _download_via_soundcloud(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting download via SoundCloud (Reserve)...")
        if not track_info.title: return DownloadResult(success=False)
        search_queries = [f"scsearch1:{track_info.uploader} - {track_info.title}", f"scsearch1:{track_info.title}"]
        for query in search_queries:
            result = await self._download_with_yt_dlp(query, target_path, "SoundCloud")
            if result.success: return result
        logger.error("All SoundCloud search attempts failed.")
        return DownloadResult(success=False)

    async def _get_track_info_from_ytmusic(self, video_id: str) -> Optional[TrackInfo]:
        # ... (unchanged)
        try:
            loop = asyncio.get_running_loop()
            song_data = await loop.run_in_executor(None, lambda: self.ytmusic.get_song(video_id))
            if not song_data or not song_data.get('videoDetails'): return None
            details = song_data['videoDetails']
            track_info = TrackInfo(identifier=details['videoId'], title=details['title'], uploader=details.get('author', ''),
                                  duration=int(details.get('lengthSeconds', 0)), url=f"https://music.youtube.com/watch?v={details['videoId']}",
                                  thumbnail_url=details['thumbnail']['thumbnails'][-1]['url'] if details.get('thumbnail') else None,
                                  source=Source.YTMUSIC)
            await self._cache.set(f"trackinfo:{video_id}", dataclasses.asdict(track_info), ttl=3600 * 24 * 7)
            return track_info
        except Exception as e:
            logger.error(f"Error reading from YTMusic details for {video_id}: {e}")
            return None

    async def _get_track_info_from_cache(self, video_id: str) -> Optional[TrackInfo]:
        # ... (unchanged)
        cached_info = await self._cache.get(f"trackinfo:{video_id}")
        if cached_info:
            return TrackInfo(**cached_info)
        return None
