import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from config import Settings
from models import DownloadResult, TrackInfo, Source
from youtube import YouTubeDownloader

logger = logging.getLogger(__name__)

class SpotifyService:
    def __init__(self, settings: Settings, youtube_downloader: YouTubeDownloader):
        self._settings = settings
        self._yt_downloader = youtube_downloader
        self._sp_client: Optional[spotipy.Spotify] = None
        
        self._initialize_spotify_client()
        logger.info("🟢 SpotifyService initialized.")

    def _initialize_spotify_client(self):
        client_id = self._settings.SPOTIFY_CLIENT_ID
        client_secret = self._settings.SPOTIFY_CLIENT_SECRET

        if not client_id or not client_secret:
            logger.warning("⚠️ SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET not set. Spotify features will be disabled.")
            return

        try:
            auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
            self._sp_client = spotipy.Spotify(auth_manager=auth_manager)
            logger.info("✅ Spotify API client created.")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Spotify API client: {e}")
            self._sp_client = None

    def _extract_spotify_id(self, url: str) -> Optional[str]:
        parsed_url = urlparse(url)
        if "open.spotify.com" in parsed_url.netloc:
            match = re.search(r"track/([a-zA-Z0-9]+)", parsed_url.path)
            if match:
                return match.group(1)
        return None
    
    def is_spotify_url(self, text: str) -> bool:
        return "open.spotify.com/track" in text

    async def download_from_url(self, spotify_url: str) -> DownloadResult:
        track_id = self._extract_spotify_id(spotify_url)
        if not track_id:
            return DownloadResult(success=False, error_message="Invalid Spotify track URL.")

        if not self._sp_client:
            return DownloadResult(success=False, error_message="Spotify API client not initialized. Check credentials.")

        try:
            loop = asyncio.get_running_loop()
            spotify_track = await loop.run_in_executor(None, self._sp_client.track, track_id)

            if not spotify_track:
                return DownloadResult(success=False, error_message="Could not fetch track info from Spotify.")

            artist_name = spotify_track['artists'][0]['name'] if spotify_track['artists'] else "Unknown Artist"
            track_name = spotify_track['name']
            query = f"{artist_name} - {track_name}"

            logger.info(f"Searching YouTube for Spotify track: '{query}'")
            # We search for more results to find a good match
            yt_tracks = await self._yt_downloader.search(query, limit=5)

            if not yt_tracks:
                return DownloadResult(success=False, error_message=f"No YouTube results found for '{query}'.")

            # Simple logic: pick the first result
            best_yt_track = yt_tracks[0]
            
            # Create a rich TrackInfo object using metadata from both services
            final_track_info = TrackInfo(
                identifier=best_yt_track.identifier, # YouTube ID
                title=track_name, # Spotify Title
                artist=artist_name, # Spotify Artist
                duration=spotify_track.get('duration_ms', 0) // 1000, # Spotify Duration
                source=Source.SPOTIFY, # Indicate origin
                thumbnail_url=spotify_track['album']['images'][0]['url'] if spotify_track.get('album', {}).get('images') else None
            )

            logger.info(f"Downloading '{final_track_info.title}' from YouTube (ID: {best_yt_track.identifier}).")
            return await self._yt_downloader.download(best_yt_track.identifier, track_info=final_track_info)

        except Exception as e:
            logger.error(f"Error downloading Spotify track '{spotify_url}': {e}", exc_info=True)
            return DownloadResult(success=False, error_message="Internal error processing Spotify track.")