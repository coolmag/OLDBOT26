import asyncio
import logging
import dataclasses
import random
from pathlib import Path
from typing import List, Optional
import urllib.parse

import httpx
import yt_dlp
from ytmusicapi import YTMusic
from config import Settings
from models import DownloadResult, TrackInfo, Source
from cache_service import CacheService

logger = logging.getLogger(__name__)

class YouTubeDownloader:
    """
    🎵 Aurora Downloader Engine (v6.2 - Triangle of Reliability, Tuned).
    This engine uses a robust, multi-source strategy that does not rely on
    unstable sources like YouTube, which are prone to IP bans in data centers.
    This version includes fine-tuned search parameters for better results.

    Download pipeline:
    1. Audius (via official API, relaxed tolerance)
    2. Bandcamp (via direct search URL)
    3. Internet Archive (via official API, relaxed query)
    4. SoundCloud (Partisan Reserve, via yt-dlp search)
    """

    def __init__(self, settings: Settings, cache_service: CacheService):
        self._settings = settings
        self._cache = cache_service
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(1)
        self.ytmusic = YTMusic()
        self.http_client = httpx.AsyncClient(timeout=15.0)

    async def search(self, query: str, limit: int = 10, **kwargs) -> List[TrackInfo]:
        """Metadata search via YTMusic, now with min/max duration filtering."""
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
                    duration = sum(int(p) * 60**i for i, p in enumerate(reversed(parts)))
                except:
                    duration = 0
                
                # Filter tracks that are too long or too short
                if not (self._settings.TRACK_MIN_DURATION_S <= duration <= self._settings.TRACK_MAX_DURATION_S):
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
        """Main download orchestrator. Follows the "Triangle of Reliability" pipeline."""
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"

        if final_path.exists() and final_path.stat().st_size > 10000:
            logger.info(f"✅ Cache hit for {video_id}")
            if not track_info:
                track_info = await self._get_track_info_from_cache(video_id)
            return DownloadResult(success=True, file_path=final_path, track_info=track_info)

        if not track_info:
            track_info = await self._get_track_info_from_cache(video_id) or await self._get_track_info_from_ytmusic(video_id)
            if not track_info:
                return DownloadResult(success=False, error_message=f"Could not get track info for {video_id}")

        async with self.semaphore:
            # 1. Audius
            audius_result = await self._download_via_audius(track_info, final_path)
            if audius_result.success:
                audius_result.track_info = track_info
                return audius_result

            # 2. Bandcamp
            bandcamp_result = await self._download_via_bandcamp(track_info, final_path)
            if bandcamp_result.success:
                bandcamp_result.track_info = track_info
                return bandcamp_result
            
            # 3. Internet Archive
            archive_result = await self._download_via_internet_archive(track_info, final_path)
            if archive_result.success:
                archive_result.track_info = track_info
                return archive_result

            # 4. SoundCloud (Partisan Reserve)
            sc_result = await self._download_via_soundcloud(track_info, final_path)
            if sc_result.success:
                sc_result.track_info = track_info
                return sc_result

        return DownloadResult(success=False, error_message="All download methods failed")

    async def _search_audius_url(self, track_info: TrackInfo) -> Optional[str]:
        """Searches Audius API for a track URL with relaxed tolerance."""
        if not track_info.title: return None
        query = f"{track_info.uploader} {track_info.title}".strip()
        try:
            resp = await self.http_client.get(
                "https://api.audius.co/v1/tracks/search",
                params={"query": query}
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("data"):
                best_match = None
                min_duration_diff = float('inf')
                
                for item in data["data"]:
                    if not item.get('permalink') or not item.get('duration'): continue
                    
                    duration_diff = abs(item['duration'] - track_info.duration)
                    if duration_diff < min_duration_diff:
                        min_duration_diff = duration_diff
                        best_match = item
                
                if best_match and min_duration_diff <= 30: # Relaxed tolerance
                    url = f"https://audius.co{best_match['permalink']}"
                    logger.info(f"🔗 Found Audius URL: {url} (duration diff: {min_duration_diff}s)")
                    return url
        except Exception as e:
            logger.error(f"Audius API search failed: {e}")
        return None

    async def _search_internet_archive_url(self, track_info: TrackInfo) -> Optional[str]:
        """Searches Internet Archive API with a more lenient query."""
        if not track_info.title: return None
        query = f'title:("{track_info.title}") AND mediatype:(audio)' # Simplified query
        try:
            resp = await self.http_client.get(
                "https://archive.org/advancedsearch.php",
                params={"q": query, "fl[]": "identifier,duration", "output": "json", "rows": "5"}
            )
            resp.raise_for_status()
            data = resp.json()
            docs = data.get('response', {}).get('docs', [])
            if docs:
                best_match = None
                min_duration_diff = float('inf')
                for item in docs:
                    try:
                        duration_parts = str(item.get('duration', '0')).split(':')
                        item_duration = sum(int(float(p)) * 60**i for i, p in enumerate(reversed(duration_parts)))
                        duration_diff = abs(item_duration - track_info.duration)
                        if duration_diff < min_duration_diff:
                            min_duration_diff = duration_diff
                            best_match = item
                    except:
                        continue
                if best_match and min_duration_diff <= 30: # Relaxed tolerance
                    identifier = best_match['identifier']
                    url = f"https://archive.org/details/{identifier}"
                    logger.info(f"🔗 Found Internet Archive URL: {url} (duration diff: {min_duration_diff}s)")
                    return url
        except Exception as e:
            logger.error(f"Internet Archive API search failed: {e}")
        return None
        
    async def _download_with_yt_dlp(self, url_or_query: str, target_path: Path, source_name: str, use_cookies: Optional[str] = None) -> DownloadResult:
        """Generic yt-dlp downloader."""
        temp_path_str = str(target_path).replace(".mp3", f"_{source_name}_temp")
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_path_str,
            'quiet': True, 'noprogress': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            'force_ipv4': True, 'sleep_interval': 3, 'max_sleep_interval': 10, 'retries': 2,
        }
        if use_cookies == "soundcloud":
            sc_cookies = self._settings.WRITABLE_DIR / "soundcloud_cookies.txt"
            if sc_cookies.exists():
                opts['cookiefile'] = str(sc_cookies)
        try:
            logger.info(f"⬇️ [{source_name}] Attempting download from: '{url_or_query}'")
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(opts).download([url_or_query])),
                timeout=90.0
            )
            paths = [Path(temp_path_str + ".mp3"), Path(temp_path_str)]
            for p in paths:
                if p.exists() and p.stat().st_size > 100_000:
                    if p != target_path:
                        if target_path.exists(): target_path.unlink(missing_ok=True)
                        p.rename(target_path)
                    logger.info(f"✅ Success via {source_name}!")
                    return DownloadResult(success=True, file_path=target_path)
            logger.warning(f"⚠️ [{source_name}] Download resulted in a file that is missing or too small.")
            return DownloadResult(success=False, error_message="File missing or too small")
        except asyncio.TimeoutError:
            logger.error(f"⏰ [{source_name}] Download timed out for: '{url_or_query}'")
            return DownloadResult(success=False, error_message="Timeout")
        except Exception as e:
            if "This video is DRM protected" in str(e):
                logger.warning(f"🛡️ [{source_name}] Failed due to DRM protection.")
            else:
                logger.error(f"❌ [{source_name}] Download failed: {e}")
            return DownloadResult(success=False, error_message=str(e))

    async def _download_via_audius(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting download via Audius...")
        url = await self._search_audius_url(track_info)
        if url:
            return await self._download_with_yt_dlp(url, target_path, "Audius")
        logger.info("Audius: No suitable track found.")
        return DownloadResult(success=False)

    async def _download_via_bandcamp(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        """Downloads from Bandcamp using a direct search URL."""
        logger.info("Attempting download via Bandcamp...")
        if not track_info.title: return DownloadResult(success=False)
        query = urllib.parse.quote(f"{track_info.uploader} {track_info.title}")
        search_url = f"https://bandcamp.com/search?q={query}"
        return await self._download_with_yt_dlp(search_url, target_path, "Bandcamp")

    async def _download_via_internet_archive(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting download via Internet Archive...")
        url = await self._search_internet_archive_url(track_info)
        if url:
            return await self._download_with_yt_dlp(url, target_path, "InternetArchive")
        logger.info("Internet Archive: No suitable track found.")
        return DownloadResult(success=False)

    async def _download_via_soundcloud(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        """(Partisan Reserve) Searches and downloads from SoundCloud."""
        logger.info("Attempting download via SoundCloud (Reserve)...")
        if not track_info.title: return DownloadResult(success=False)
        search_queries = [ f"scsearch1:{track_info.uploader} - {track_info.title}", f"scsearch1:{track_info.title}",]
        for query in search_queries:
            result = await self._download_with_yt_dlp(query, target_path, "SoundCloud", use_cookies="soundcloud")
            if result.success:
                return result
        logger.error("All SoundCloud search attempts failed.")
        return DownloadResult(success=False)

    async def _get_track_info_from_ytmusic(self, video_id: str) -> Optional[TrackInfo]:
        """Gets track metadata from YTMusic API."""
        try:
            loop = asyncio.get_running_loop()
            song_data = await loop.run_in_executor(None, lambda: self.ytmusic.get_song(video_id))
            if not song_data or not song_data.get('videoDetails'): return None
            details = song_data['videoDetails']
            track_info = TrackInfo(
                identifier=details['videoId'], title=details['title'], uploader=details.get('author', ''),
                duration=int(details.get('lengthSeconds', 0)), url=f"https://music.youtube.com/watch?v={details['videoId']}",
                thumbnail_url=details['thumbnail']['thumbnails'][-1]['url'] if details.get('thumbnail') else None,
                source=Source.YTMUSIC
            )
            await self._cache.set(f"trackinfo:{video_id}", dataclasses.asdict(track_info), ttl=3600 * 24 * 7)
            return track_info
        except Exception as e:
            logger.error(f"Error reading from YTMusic details for {video_id}: {e}")
            return None

    async def _get_track_info_from_cache(self, video_id: str) -> Optional[TrackInfo]:
        """Gets track metadata from local cache."""
        cached_info = await self._cache.get(f"trackinfo:{video_id}")
        if cached_info:
            return TrackInfo(**cached_info)
        return None
