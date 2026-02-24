import logging
import random
from typing import List, Optional

import httpx

from config import Settings
from models import DownloadResult, TrackInfo, Source

logger = logging.getLogger("cobalt")


class CobaltDownloader:
    """
    Downloader that uses Cobalt API instances.
    Offloads downloading and conversion to a third-party server.
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._instances = self._settings.COBALT_INSTANCES
        if not self._instances:
            logger.warning("No Cobalt instances configured. Cobalt downloader is disabled.")

    async def download(self, video_id: str) -> Optional[DownloadResult]:
        if not self._instances:
            return None

        youtube_url = f"https://www.youtube.com/watch?v={video_id}"
        
        # Shuffle instances to distribute load
        random.shuffle(self._instances)

        async with httpx.AsyncClient(timeout=45.0) as client:
            for instance in self._instances:
                try:
                    logger.info(f"Trying Cobalt instance: {instance}")
                    payload = {
                        "url": youtube_url,
                        "vQuality": "720",  # Request a video quality to ensure audio is available
                        "aFormat": "mp3",
                        "isAudioOnly": True,
                    }
                    headers = {"Accept": "application/json", "Content-Type": "application/json"}
                    
                    response = await client.post(f"{instance}/api/json", json=payload, headers=headers)
                    response.raise_for_status()
                    
                    data = response.json()

                    if data.get("status") == "stream":
                        logger.success(f"Cobalt returned a stream URL: {data['url']}")
                        # Cobalt doesn't provide track metadata, so we create a partial DownloadResult
                        return DownloadResult(
                            success=True,
                            file_path=data["url"], # This is a URL, not a local path
                            track_info=None, # Caller must fetch metadata if needed
                            is_url=True 
                        )
                    elif data.get("status") == "error":
                        logger.warning(f"Cobalt instance {instance} returned an error: {data.get('text')}")
                        continue # Try next instance
                    else:
                        logger.warning(f"Cobalt instance {instance} returned unexpected status: {data.get('status')}")

                except httpx.RequestError as e:
                    logger.warning(f"Request to Cobalt instance {instance} failed: {e}")
                    continue # Try next instance
                except Exception as e:
                    logger.error(f"An unexpected error occurred with Cobalt instance {instance}: {e}", exc_info=True)
                    continue

        logger.error("All Cobalt instances failed for URL: {youtube_url}")
        return None

