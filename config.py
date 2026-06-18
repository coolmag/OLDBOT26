from pathlib import Path
from typing import List, Any, Optional, Union
from functools import lru_cache
import logging
import json
import os

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, ValidationInfo

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
    
    BOT_TOKEN: str 
    TELEGRAM_SECRET: Optional[str] = None
    WEBHOOK_URL: str = ""
    BASE_URL: str = ""
    PLAYER_URL: str = ""
    ADMIN_IDS: str = ""
    
    COOKIES_CONTENT: str = "" # Старая переменная, можно оставить для совместимости или удалить
    YT_COOKIES: str = ""
    SC_COOKIES: str = ""
    PO_TOKEN: Optional[str] = None
    VISITOR_DATA: Optional[str] = None
    PROXY_URL: Optional[str] = None
    SPOTIFY_CLIENT_ID: Optional[str] = None
    SPOTIFY_CLIENT_SECRET: Optional[str] = None
    OPENROUTER_API_KEY: str = ""
    JAMENDO_CLIENT_ID: str = ""
    REDIS_URL: Optional[str] = os.getenv("REDIS_URL", None)
    
    COBALT_INSTANCES: str = "https://api.cobalt.tools,https://cobalt.api.timelessnesses.me"
    PIPED_INSTANCES: str = "https://pipedapi.adminforge.de,https://pipedapi.kavin.rocks,https://api-piped.mha.fi,https://pipedapi.drgns.space,https://pipedapi.leptons.xyz"
    INVIDIOUS_INSTANCES: str = "https://invidious.fdn.fr,https://yt.artemislena.eu,https://invidious.protokolla.fi,https://invidious.privacyredirect.com"

    GOOGLE_API_KEY: str = ""
    VK_LOGIN: Optional[str] = None
    VK_PASSWORD: Optional[str] = None
    ADMIN_ID_LIST: List[int] = []
    
    BASE_DIR: Path = Path(__file__).resolve().parent

    # Определяем базовую директорию для записи. На Vercel это /tmp.
    WRITABLE_DIR: Path = Path("/tmp") if os.getenv("VERCEL") == "1" else BASE_DIR

    DOWNLOADS_DIR: Path = WRITABLE_DIR / "downloads"
    TEMP_AUDIO_DIR: Path = WRITABLE_DIR / "temp_audio"
    CACHE_DB_PATH: Path = WRITABLE_DIR / "cache.db"
    DB_PATH: Path = WRITABLE_DIR / "bot.db"
    COOKIES_FILE: Path = WRITABLE_DIR / "cookies.txt"
    YTDLP_COOKIES_FILE: Optional[Path] = None # Новое поле для cookies.txt yt-dlp
    PROXIES_FILE: Path = WRITABLE_DIR / "working_proxies.txt"
    V2RAY_PROXIES_FILE: Path = WRITABLE_DIR / "hiddify_compatible_v2ray_proxies.txt"
    
    LOG_LEVEL: str = "INFO"
    MAX_CONCURRENT_DOWNLOADS: int = 3
    DOWNLOAD_TIMEOUT: int = 120
    TRACK_MAX_DURATION_S: int = 900
    TRACK_MIN_DURATION_S: int = 60
    ENABLE_AI_DJ_INTRO: bool = False # Включает/выключает генерацию голосовой подводки от AI DJ перед треком


    @field_validator("YTDLP_COOKIES_FILE", mode="before")
    @classmethod
    def _parse_yt_dlp_cookies_file(cls, v: Any) -> Optional[Path]:
        if v is None or not v.strip(): return None
        return Path(v)

    @field_validator("ADMIN_ID_LIST", mode="before")
    @classmethod
    def _assemble_admin_ids(cls, v: Any, info: ValidationInfo) -> List[int]:
        if not v: return []
        try: return [int(i.strip()) for i in str(v).split(",") if i.strip()]
        except: return []

@lru_cache()
def get_settings() -> Settings:
    return Settings()
