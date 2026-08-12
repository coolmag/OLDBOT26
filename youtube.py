import asyncio
import logging
import dataclasses
import random
import subprocess
import sys
import importlib
import shutil
from pathlib import Path
from typing import List, Optional, Tuple
import json
import urllib.parse

import httpx
import yt_dlp
from ytmusicapi import YTMusic

from config import Settings
from models import DownloadResult, TrackInfo, Source
from cache_service import CacheService
from jamendo import JamendoClient
from openverse import OpenverseClient

logger = logging.getLogger(__name__)

class YouTubeDownloader:
    """
    🎵 Aurora Downloader Engine (v7.6 - Hardlink Plugin & Node Check).
    """

    def __init__(self, settings: Settings, cache_service: CacheService):
        self._settings = settings
        self._cache = cache_service
        self.jamendo = JamendoClient(settings.JAMENDO_CLIENT_ID)
        self.openverse = OpenverseClient()
        self._settings.DOWNLOADS_DIR.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(1)
        self.ytmusic = YTMusic()
        self.http_client = httpx.AsyncClient(timeout=30.0, verify=False)

        self.piped_instances = settings.PIPED_INSTANCES
        self.cobalt_instances = settings.COBALT_INSTANCES
        self.invidious_instances = settings.INVIDIOUS_INSTANCES

        self.po_token = getattr(settings, 'PO_TOKEN', None)
        self.visitor_data = getattr(settings, 'VISITOR_DATA', None)

        self.yt_cookies_path = self._settings.WRITABLE_DIR / "youtube_cookies.txt"
        self.sc_cookies_path = self._settings.WRITABLE_DIR / "soundcloud_cookies.txt"

        if self._settings.YT_COOKIES:
            with open(self.yt_cookies_path, "w", encoding="utf-8") as f:
                f.write(self._settings.YT_COOKIES)
        
        if self._settings.SC_COOKIES:
            with open(self.sc_cookies_path, "w", encoding="utf-8") as f:
                f.write(self._settings.SC_COOKIES)

        # 🛠️ RUNTIME DIAGNOSTICS & FIX
        # 1. Проверяем, есть ли Node.js в PATH (без него yt-dlp не сможет решать JS)
        node_path = shutil.which("node") or shutil.which("nodejs")
        if not node_path:
            logger.error("❌ CRITICAL: Node.js not found in PATH! yt-dlp cannot solve YouTube JS challenges.")
        else:
            logger.info(f"✅ Node.js found in system at: {node_path}")

        # 2. Жесткая привязка yt-dlp-ejs (обход сломанных entry_points)
        try:
            ejs_mod = importlib.import_module("yt_dlp_plugins.extractor.ejs")
            ejs_file = Path(ejs_mod.__file__)
            # Находим корень папки yt_dlp_plugins
            source_plugin_root = ejs_file.parent.parent 
            
            # Целевая папка, которую yt-dlp читает ВСЕГДА
            target_plugin_root = Path.home() / ".yt-dlp" / "plugins" / "yt_dlp_plugins"
            
            if not target_plugin_root.exists():
                logger.info("🔧 yt-dlp entry_points ignored. Manually linking yt-dlp-ejs to ~/.yt-dlp/plugins/...")
                shutil.copytree(source_plugin_root, target_plugin_root)
                logger.info("✅ yt-dlp-ejs successfully linked! YouTube Signature solving fixed.")
            else:
                logger.info("✅ yt-dlp-ejs is already linked in ~/.yt-dlp/plugins/.")
        except ImportError:
            logger.error("❌ CRITICAL: yt-dlp-ejs is NOT installed in the environment!")
        except Exception as e:
            logger.error(f"❌ Failed to link yt-dlp-ejs: {e}")

    async def _check_instance_health(self, instance: str, endpoint: str = "/") -> bool:
        try:
            check_url = f"{instance.rstrip('/')}{endpoint}"
            async with self.http_client.stream("HEAD", check_url, timeout=3.0, follow_redirects=True) as response:
                if 200 <= response.status_code < 400:
                    return True
                return False
        except Exception:
            return False

    async def search(self, query: str, limit: int = 10, **kwargs) -> List[TrackInfo]:
        if kwargs.get('decade'): query = f"{query} {kwargs['decade']}"
        if not query or not query.strip(): return []
        logger.info(f"🔎 YTMusic Search: {query}")
        loop = asyncio.get_running_loop()
        
        try:
            search_results = await loop.run_in_executor(None, lambda: self.ytmusic.search(query, filter="songs", limit=limit))
        except Exception as e:
            try:
                search_results = await loop.run_in_executor(None, lambda: self.ytmusic.search(query, limit=limit))
            except Exception as e2:
                logger.error(f"❌ YTMusic Search completely failed: {e2}", exc_info=True)
                return []

        results = []
        for item in search_results:
            video_id = item.get('videoId')
            if not video_id: continue
            artists = ", ".join([a['name'] for a in item.get('artists', [])])
            duration_text = item.get('duration', '0:00')
            try:
                parts = duration_text.split(':')
                duration = sum(int(p) * 60**i for i, p in enumerate(reversed(parts)))
            except (ValueError, IndexError): duration = 0
            if not (self._settings.TRACK_MIN_DURATION_S <= duration <= self._settings.TRACK_MAX_DURATION_S): continue
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

    async def download(self, video_id: str, track_info: Optional[TrackInfo] = None) -> DownloadResult:
        final_path = self._settings.DOWNLOADS_DIR / f"{video_id}.mp3"
        if final_path.exists():
            if not track_info: track_info = await self._get_track_info_from_cache(video_id)
            return DownloadResult(success=True, file_path=final_path, track_info=track_info)

        if not track_info:
            track_info = await self._get_track_info_from_cache(video_id) or await self._get_track_info_from_ytmusic(video_id)
            if not track_info: return DownloadResult(success=False, error_message=f"Could not get track info for {video_id}")

        await self._cleanup_old_downloads()

        async with self.semaphore:
            methods = [
                self._download_via_ytdlp_youtube,
                self._download_via_soundcloud,
                self._download_via_piped,
                self._download_via_cobalt,
                self._download_via_invidious,
                self._download_via_jamendo,
                self._download_via_openverse,
                self._download_via_audius,
                self._download_via_internet_archive,
            ]
            
            for method in methods:
                if method.__name__ == "_download_via_jamendo" and not self._settings.JAMENDO_CLIENT_ID:
                    continue

                logger.info(f"🚀 Trying {method.__name__}...")
                try:
                    result = await method(track_info, final_path)
                    if result.success:
                        result.track_info = track_info
                        return result
                    logger.warning(f"⚠️ {method.__name__} failed: {result.error_message}")
                except Exception as e:
                    logger.error(f"❌ {method.__name__} exception: {e}", exc_info=True)
        
        return DownloadResult(success=False, error_message="All download methods failed")

    async def _cleanup_old_downloads(self, limit_mb: int = 400):
        files = sorted(list(self._settings.DOWNLOADS_DIR.glob("*.mp3")), key=lambda f: f.stat().st_mtime)
        total_size = sum(f.stat().st_size for f in files)
        
        if total_size > limit_mb * 1024 * 1024:
            for file in files:
                if total_size <= limit_mb * 1024 * 1024 * 0.8:
                    break
                try:
                    file.unlink()
                    total_size -= file.stat().st_size
                except Exception: pass

    def _validate_audio(self, path: Path) -> Tuple[bool, str]:
        min_dur = getattr(self._settings, 'TRACK_MIN_DURATION_S', 60)
        try:
            duration_str = subprocess.check_output(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                timeout=10, stderr=subprocess.DEVNULL
            ).strip().decode('utf-8')
            duration = float(duration_str)
            if duration < min_dur: return False, f"Short duration ({duration:.1f}s < {min_dur}s)"

            bitrate_str = subprocess.check_output(
                ['ffprobe', '-v', 'error', '-show_entries', 'stream=bit_rate', '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                timeout=10, stderr=subprocess.DEVNULL
            ).strip().decode('utf-8')
            if bitrate_str and bitrate_str.isdigit():
                if float(bitrate_str) / 1000 < 128: return False, "Low bitrate"
            return True, ""
        except Exception as e: return False, f"ffprobe validation failed: {e}"

    async def _download_direct_http(self, audio_url: str, target_path: Path, source_name: str, extra_headers: dict = None) -> DownloadResult:
        temp_path = target_path.with_suffix('.part')
        try:
            async with self.http_client.stream("GET", audio_url, follow_redirects=True, headers=extra_headers) as response:
                response.raise_for_status()
                with open(temp_path, "wb") as f:
                    async for chunk in response.aiter_bytes(): f.write(chunk)
            
            valid, msg = await asyncio.to_thread(self._validate_audio, temp_path)
            if not valid:
                temp_path.unlink(missing_ok=True)
                return DownloadResult(success=False, error_message=f"Quality check failed: {msg}")

            temp_path.rename(target_path)
            logger.info(f"✅ Success via {source_name}!")
            return DownloadResult(success=True, file_path=target_path)
        except Exception as e:
            if temp_path.exists(): temp_path.unlink(missing_ok=True)
            return DownloadResult(success=False, error_message=str(e))

    async def _download_via_jamendo(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting Jamendo...")
        audio_url = await self.jamendo.search_track_url(track_info.uploader, track_info.title)
        if audio_url: return await self._download_direct_http(audio_url, target_path, "Jamendo")
        return DownloadResult(success=False, error_message="Not found on Jamendo")

    async def _download_via_openverse(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting Openverse...")
        query = f"{track_info.uploader} {track_info.title}".strip()
        audio_url = await self.openverse.search_track_url(query)
        if audio_url: return await self._download_direct_http(audio_url, target_path, "Openverse")
        return DownloadResult(success=False, error_message="Not found on Openverse")

    async def _download_via_audius(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting Audius...")
        query = f"{track_info.uploader} {track_info.title}".strip()
        try:
            resp = await self.http_client.get(f"https://api.audius.co/v1/tracks/search?query={urllib.parse.quote(query)}&app_name=AuroraDownloader")
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if not data: return DownloadResult(success=False, error_message="No results on Audius")

            best_match = min(data, key=lambda x: abs(x.get('duration', 0) - track_info.duration), default=None)
            if best_match and abs(best_match.get('duration', 0) - track_info.duration) <= 60:
                track_id = best_match['id']
                stream_resp = await self.http_client.get(f"https://api.audius.co/v1/tracks/{track_id}/stream?app_name=AuroraDownloader", follow_redirects=False)
                if 300 <= stream_resp.status_code < 400 and 'Location' in stream_resp.headers:
                    return await self._download_direct_http(stream_resp.headers['Location'], target_path, "Audius")
        except Exception: pass
        return DownloadResult(success=False, error_message="Audius failed")

    async def _download_via_ytdlp_youtube(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting yt-dlp direct YouTube...")
        video_url = f"https://www.youtube.com/watch?v={track_info.identifier}"
        return await self._download_with_yt_dlp(video_url, target_path, "YouTube")

    async def _download_via_piped(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting Piped...")
        video_id = track_info.identifier
        health_checks = [self._check_instance_health(instance, f"/streams/{video_id}") for instance in self.piped_instances]
        results = await asyncio.gather(*health_checks)
        healthy_instances = [instance for instance, is_healthy in zip(self.piped_instances, results) if is_healthy]

        for instance in healthy_instances:
            try:
                resp = await self.http_client.get(f"{instance}/streams/{video_id}", timeout=15.0)
                if resp.status_code != 200: continue
                audio_streams = resp.json().get("audioStreams", [])
                if not audio_streams: continue
                best_stream = max(audio_streams, key=lambda x: x.get('bitrate', 0))
                audio_url = best_stream.get('url')
                if audio_url:
                    result = await self._download_direct_http(audio_url, target_path, f"Piped({instance.split('//')[1]})")
                    if result.success: return result
            except Exception: continue
        return DownloadResult(success=False, error_message="All healthy Piped instances failed")

    async def _download_via_cobalt(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting Cobalt...")
        video_url = f"https://www.youtube.com/watch?v={track_info.identifier}"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        health_checks = [self._check_instance_health(instance) for instance in self.cobalt_instances]
        results = await asyncio.gather(*health_checks)
        healthy_instances = [instance for instance, is_healthy in zip(self.cobalt_instances, results) if is_healthy]

        for instance in healthy_instances:
            try:
                payload = {"url": video_url, "downloadMode": "audio", "audioFormat": "mp3"}
                resp = await self.http_client.post(f"{instance.rstrip('/')}/api/json", json=payload, headers=headers, timeout=30.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") in ["stream", "redirect", "tunnel"] and data.get("url"):
                        return await self._download_direct_http(data.get("url"), target_path, f"Cobalt({instance.split('//')[1]})")
            except Exception: continue
        return DownloadResult(success=False, error_message="All healthy Cobalt instances failed")

    async def _download_via_invidious(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting Invidious...")
        video_id = track_info.identifier
        health_checks = [self._check_instance_health(instance, f"/api/v1/videos/{video_id}") for instance in self.invidious_instances]
        results = await asyncio.gather(*health_checks)
        healthy_instances = [instance for instance, is_healthy in zip(self.invidious_instances, results) if is_healthy]

        for instance in healthy_instances:
            try:
                resp = await self.http_client.get(f"{instance}/api/v1/videos/{video_id}", timeout=15.0)
                if resp.status_code != 200: continue
                audio_streams = resp.json().get("adaptiveFormats", [])
                audio_only = [s for s in audio_streams if 'audio' in s.get('type', '').lower()]
                if not audio_only: continue
                best_stream = max(audio_only, key=lambda x: x.get('bitrate', 0))
                audio_url = best_stream.get('url')
                if audio_url:
                    result = await self._download_direct_http(audio_url, target_path, f"Invidious({instance.split('//')[1]})")
                    if result.success: return result
            except Exception: continue
        return DownloadResult(success=False, error_message="All healthy Invidious instances failed")

    async def _download_via_internet_archive(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting Internet Archive...")
        query = f"{track_info.uploader} {track_info.title}"
        try:
            params = {"q": f"{query} AND mediatype:(audio) AND format:(mp3)", "fl[]": ["identifier", "title"], "output": "json", "rows": "1"}
            resp = await self.http_client.get("https://archive.org/advancedsearch.php", params=params)
            resp.raise_for_status()
            data = resp.json().get("response", {}).get("docs", [])
            if not data: return DownloadResult(success=False, error_message="Not found on IA")
            identifier = data[0]['identifier']
            files_resp = await self.http_client.get(f"https://archive.org/metadata/{identifier}/files")
            files_resp.raise_for_status()
            files = files_resp.json().get("result", [])
            mp3_file = next((f for f in files if f['name'].endswith('.mp3') and len(f['name']) < 100), None)
            if not mp3_file: return DownloadResult(success=False, error_message="No valid MP3 on IA")
            audio_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(mp3_file['name'])}"
            return await self._download_direct_http(audio_url, target_path, "InternetArchive", extra_headers={"User-Agent": "AuroraBot/1.0"})
        except Exception as e:
            return DownloadResult(success=False, error_message=str(e))

    async def _download_via_soundcloud(self, track_info: TrackInfo, target_path: Path) -> DownloadResult:
        logger.info("Attempting SoundCloud...")
        if not track_info.title: return DownloadResult(success=False, error_message="No title")
        search_queries = [
            f"scsearch1:{track_info.uploader} - {track_info.title}",
            f"scsearch1:{track_info.title}"
        ]
        for query in search_queries:
            result = await self._download_with_yt_dlp(query, target_path, "SoundCloud")
            if result.success: return result
        return DownloadResult(success=False, error_message="SoundCloud failed")

    async def _download_with_yt_dlp(self, url_or_query: str, target_path: Path, source_name: str) -> DownloadResult:
        temp_path = target_path.with_name(f"{target_path.stem}_{source_name}_temp")
        temp_path_str = str(temp_path)
        
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': temp_path_str,
            'quiet': True,
            'noprogress': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192'
            }],
            'force_ipv4': True,
            'socket_timeout': 30,
            'retries': 3,
            'retry_sleep_functions': {'http': 10},
            'ignoreerrors': True,
            # js_runtimes убран, yt-dlp сам найдет node и ~/.yt-dlp/plugins/
        }
        
        cookie_file = None
        if source_name == "SoundCloud":
            if self.sc_cookies_path.exists() and self.sc_cookies_path.stat().st_size > 0:
                cookie_file = self.sc_cookies_path
        elif "YouTube" in source_name:
            if self.yt_cookies_path.exists() and self.yt_cookies_path.stat().st_size > 0:
                cookie_file = self.yt_cookies_path

        if cookie_file:
            opts['cookiefile'] = str(cookie_file)

        is_valid_token = (
            self.po_token and 
            self.visitor_data and 
            len(str(self.po_token)) > 50 and 
            "=" not in str(self.po_token) and
            "+" not in str(self.po_token) and
            "/" not in str(self.po_token)
        )

        youtube_args = {}
        
        if cookie_file and "YouTube" in source_name:
            youtube_args['player_client'] = ['web']
            if is_valid_token:
                youtube_args['player_client'].extend(['mweb', 'web_creator'])
                youtube_args['po_token'] = [
                    f'web.gvs+{self.po_token}', 
                    f'mweb.gvs+{self.po_token}', 
                    f'web_creator.gvs+{self.po_token}'
                ]
                youtube_args['visitor_data'] = self.visitor_data
        else:
            youtube_args['player_client'] = ['ios', 'android_vr', 'web_embedded']

        opts['extractor_args'] = {'youtube': youtube_args}

        try:
            await asyncio.to_thread(yt_dlp.YoutubeDL(opts).download, [url_or_query])
            final_temp_path = temp_path.with_suffix('.mp3') if temp_path.with_suffix('.mp3').exists() else temp_path
            if not final_temp_path.exists(): raise FileNotFoundError("yt-dlp did not produce output")

            valid, msg = await asyncio.to_thread(self._validate_audio, final_temp_path)
            if not valid:
                final_temp_path.unlink(missing_ok=True)
                return DownloadResult(success=False, error_message=f"Quality check failed: {msg}")

            final_temp_path.rename(target_path)
            return DownloadResult(success=True, file_path=target_path)
        except Exception as e:
            logger.error(f"❌ [{source_name}] yt-dlp failed: {e}")
            return DownloadResult(success=False, error_message=str(e))

    async def _get_track_info_from_ytmusic(self, video_id: str) -> Optional[TrackInfo]:
        try:
            loop = asyncio.get_running_loop()
            song_data = await loop.run_in_executor(None, lambda: self.ytmusic.get_song(video_id))
            if not song_data or not song_data.get('videoDetails'): return None
            details = song_data['videoDetails']
            track_info = TrackInfo(
                identifier=details['videoId'],
                title=details['title'],
                uploader=details.get('author', ''),
                duration=int(details.get('lengthSeconds', 0)),
                url=f"https://music.youtube.com/watch?v={details['videoId']}",
                thumbnail_url=details['thumbnail']['thumbnails'][-1]['url'] if details.get('thumbnail') else None,
                source=Source.YTMUSIC
            )
            await self._cache.set(f"trackinfo:{video_id}", dataclasses.asdict(track_info), ttl=3600 * 24 * 7)
            return track_info
        except Exception as e:
            logger.error(f"Error reading YTMusic for {video_id}: {e}")
            return None

    async def _get_track_info_from_cache(self, video_id: str) -> Optional[TrackInfo]:
        cached_info = await self._cache.get(f"trackinfo:{video_id}")
        if cached_info:
            return TrackInfo(**cached_info)
        return None
