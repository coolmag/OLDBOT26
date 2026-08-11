import httpx
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class OpenverseClient:
    """
    A client for interacting with the Openverse API to find and retrieve
    direct audio URLs for tracks. No API key is required.
    """
    BASE_URL = "https://api.openverse.org"

    def __init__(self):
        self.http = httpx.AsyncClient(timeout=15)

    async def search_track_url(self, query: str) -> Optional[str]:
        """
        Searches for a track on Openverse and returns a direct audio URL
        for the best match if found.
        """
        if not query:
            return None
            
        try:
            # We add "source:jamendo" or other high-quality sources to the query
            # to filter out low-quality results like sound effects.
            # licenses = "commercial,modification"
            resp = await self.http.get(
                f"{self.BASE_URL}/v1/audio/",
                params={
                    "q": query,
                    "page_size": 3,
                    "license_type": "all-creative-commons",
                }
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            
            if not results:
                logger.info(f"Openverse: No results for query '{query}'")
                return None
            
            # Openverse results can be noisy, so we just take the first one for now.
            best_match = results[0]
            audio_url = best_match.get("url")
            
            if audio_url:
                logger.info(f"🔗 Found Openverse URL: {audio_url}")
                return audio_url
            
        except httpx.HTTPStatusError as e:
            logger.error(f"Openverse API returned an error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logger.error(f"An unexpected error occurred during Openverse search: {e}")

        return None
