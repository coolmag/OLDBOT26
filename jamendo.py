import httpx
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class JamendoClient:
    """
    A client for interacting with the Jamendo API to find and retrieve
    direct audio URLs for tracks.
    """
    BASE_URL = "https://api.jamendo.com/v3.0"

    def __init__(self, client_id: str):
        # The Jamendo client_id is required. Get one for free at dev.jamendo.com
        if not client_id:
            logger.warning("Jamendo client_id is not configured. Jamendo source will be unavailable.")
        self.client_id = client_id
        self.http = httpx.AsyncClient(timeout=15)

    async def search_track_url(self, artist: str, title: str) -> Optional[str]:
        """
        Searches for a track on Jamendo and returns a direct audio URL
        for the best match if found.
        """
        if not self.client_id:
            return None

        query = f"{artist} {title}".strip()
        if not query:
            return None
            
        try:
            resp = await self.http.get(
                f"{self.BASE_URL}/tracks",
                params={
                    "client_id": self.client_id,
                    "format": "json",
                    "search": query,
                    "limit": 3, # Limit to a few results for speed
                    "audioformat": "mp32"   # Request 192kbps quality
                }
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            
            if not results:
                logger.info(f"Jamendo: No results for query '{query}'")
                return None
            
            # For now, we take the first result as the "best" match.
            # A more advanced implementation could compare duration.
            best_match = results[0]
            audio_url = best_match.get("audio")
            
            if audio_url:
                logger.info(f"🔗 Found Jamendo URL: {audio_url}")
                return audio_url
            
        except httpx.HTTPStatusError as e:
            logger.error(f"Jamendo API returned an error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logger.error(f"An unexpected error occurred during Jamendo search: {e}")

        return None
