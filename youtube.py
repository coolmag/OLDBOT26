import asyncio
import logging
import random
import subprocess
import difflib
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
import yt_dlp
from ytmusicapi import YTMusic

from config import Settings
from models import DownloadResult, TrackInfo, Source
from cache_service import CacheService
from jamendo import JamendoClient
from openverse import OpenverseClient
from db_service import DatabaseService
from event_bus import EventBus

logger = logging.getLogger(__name__)

class YouTubeDownloader:
    def __init__(self, settings: Settings, cache_service: CacheService, db_service: DatabaseService, event_bus: EventBus):
        self._settings = settings
        self._cache = cache_service
        self._db = db_service
        self._event_bus = event_bus
        self.jamendo = JamendoClient(settings.JAMENDO_CLIENT_ID)
        self.openverse = OpenverseClient()
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(1)
        self.ytmusic = YTMusic()
        self.http_client = httpx.AsyncClient(timeout=20.0)

        self.yt_cookies_path = self._settings.WRITABLE_DIR / "youtube_cookies.txt"
        self.sc_cookies_path = self._settings.WRITABLE_DIR / "soundcloud_cookies.txt"

        if self._settings.YT_COOKIES:
            with open(self.yt_cookies_path, "w", encoding="utf-8") as f:
                f.write(self._settings.YT_COOKIES)
        
        if self._settings.SC_COOKIES:
            with open(self.sc_cookies_path, "w", encoding="utf-8") as f:
                f.write(self._settings.SC_COOKIES)

    async def search(self, query: str, limit: int = 10, **kwargs) -> List[TrackInfo]:
        if kwargs.get('decade'): query = f"{query} {kwargs['decade']}"
        if not query or not query.strip(): return []
        logger.info(f"🔎 YTMusic Search: {query}")
        loop = asyncio.get_running_loop()
        
        try:
            search_results = await loop.run_in_executor(None, lambda: self.ytmusic.search(query, filter="songs", limit=limit))
        except Exception as e:
            logger.warning(f"⚠️ YTMusic Search failed: {e}")
            return []

        results = []
        for item in search_results:
            video_id = item.get('videoId')
            if not video_id: continue
            
            title = item.get('title', '')
            similarity = difflib.SequenceMatcher(None, query.lower(), title.lower()).ratio()
            if similarity < 0.2: continue

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

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"
        if final_path.exists():
            return DownloadResult(success=True, file_path=final_path, track_info=track_info)

        if not track_info:
            track_info = await self._get_track_info_from_cache(video_id) or await self._get_track_info_from_ytmusic(video_id)
            if not track_info: return DownloadResult(success=False, error_message=f"Could not get track info for {video_id}")

        async with self.semaphore:
            # ПРИОРИТЕТ: SoundCloud -> Audius -> InternetArchive -> YouTube -> Jamendo
            methods = [
                self._download_via_soundcloud,
                self._download_via_audius,
                self._download_via_internet_archive,
                self._download_via_youtube,
                self._download_via_jamendo
            ]
            for method in methods:
                if method.__name__ == "_download_via_jamendo" and not self._settings.JAMENDO_CLIENT_ID: continue
                
                result = await method(track_info, final_path)
                if result.success:
                    result.track_info = track_info
                    return result
                else:
                    await self._cache.record_failure(video_id)
                    logger.warning(f"⚠️ {method.__name__} failed.")
        return DownloadResult(success=False, error_message="All download methods failed")

    def _validate_audio(self, path: Path) -> Tuple[bool, str]:
        min_dur = getattr(self._settings, 'TRACK_MIN_DURATION_S', 60)
        try:
            duration_str = subprocess.check_output(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                timeout=10, stderr=subprocess.DEVNULL
            ).strip().decode('utf-8')
            if float(duration_str) < min_dur: return False, f"Short duration ({float(duration_str):.1f}s)"
            return True, ""
        except Exception as e: return False, f"ffprobe failed: {e}"

    async def _download_with_yt_dlp(self, url_or_query: str, target_path: Path, source_name: str) -> DownloadResult:
        opts_sim = {'quiet': True, 'simulate': True, 'force_ipv4': True}
        if source_name == "SoundCloud": opts_sim['cookiefile'] = str(self.sc_cookies_path)
        elif "YouTube" in source_name: opts_sim['cookiefile'] = str(self.yt_cookies_path)

        try:
            info = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(opts_sim).extract_info(url_or_query, download=False))
            duration = info.get('duration', 0)
            if duration < self._settings.TRACK_MIN_DURATION_S:
                return DownloadResult(success=False, error_message="Preview detected")
            if info.get('is_live'): return DownloadResult(success=False, error_message="Live stream")
        except Exception as e:
            return DownloadResult(success=False, error_message=str(e))

        temp_path_str = str(target_path).replace(".mp3", f"_{source_name}_temp")
        opts = {'format': 'bestaudio/best', 'outtmpl': temp_path_str, 'quiet': True, 'noprogress': True,
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
                'force_ipv4': True}
        if source_name == "SoundCloud": opts['cookiefile'] = str(self.sc_cookies_path)
        elif "YouTube" in source_name: opts['cookiefile'] = str(self.yt_cookies_path)
        
        try:
            await asyncio.to_thread(yt_dlp.YoutubeDL(opts).download, [url_or_query])
            final_temp_path = Path(temp_path_str + ".mp3")
            if not final_temp_path.exists(): raise FileNotFoundError("Download failed")
            
            valid, msg = await asyncio.to_thread(self._validate_audio, final_temp_path)
            if not valid:
                final_temp_path.unlink()
                return DownloadResult(success=False, error_message=msg)

            final_temp_path.rename(target_path)
            return DownloadResult(success=True, file_path=target_path)
        except Exception as e:
            return DownloadResult(success=False, error_message=str(e))

    async def _download_via_youtube(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info(f"🚀 Trying direct YouTube download: {track_info.identifier}")
        return await self._download_with_yt_dlp(f"https://www.youtube.com/watch?v={track_info.identifier}", target_path, "YouTubeDirect")

    async def _download_via_soundcloud(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        return await self._download_with_yt_dlp(f"scsearch1:{track_info.title}", target_path, "SoundCloud")

    async def _download_via_audius(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        return DownloadResult(success=False)

    async def _download_via_internet_archive(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        return DownloadResult(success=False)

    async def _download_via_jamendo(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        return DownloadResult(success=False)

    async def _get_track_info_from_cache(self, video_id: str) -> Optional[TrackInfo]:
        track = await self._db.get_track(video_id)
        if track: return track
        cached_info = await self._cache.get(f"trackinfo:{video_id}")
        return TrackInfo(**cached_info) if cached_info else None

    async def _get_track_info_from_ytmusic(self, video_id: str) -> Optional[TrackInfo]:
        try:
            song_data = await asyncio.to_thread(self.ytmusic.get_song, video_id)
            details = song_data['videoDetails']
            track_info = TrackInfo(identifier=details['videoId'], title=details['title'], uploader=details.get('author', ''),
                                  duration=int(details.get('lengthSeconds', 0)), source=Source.YTMUSIC)
            await self._db.save_track(track_info)
            return track_info
        except: return None
