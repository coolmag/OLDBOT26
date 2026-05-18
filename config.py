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
    
    COOKIES_CONTENT: str = ""
    PO_TOKEN: Optional[str] = None
    VISITOR_DATA: Optional[str] = None
    PROXY_URL: Optional[str] = None
    SPOTIFY_CLIENT_ID: Optional[str] = None
    SPOTIFY_CLIENT_SECRET: Optional[str] = None
    OPENROUTER_API_KEY: str = ""
    JAMENDO_CLIENT_ID: str = ""

    
    COBALT_INSTANCES: Union[List[str], str, None] = None
    PIPED_INSTANCES: Union[List[str], str, None] = None
    INVIDIOUS_INSTANCES: Union[List[str], str, None] = None

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

    @field_validator("COBALT_INSTANCES", "PIPED_INSTANCES", "INVIDIOUS_INSTANCES", mode="before")
    @classmethod
    def _parse_instances(cls, v: Any, info: ValidationInfo) -> List[str]:
        defaults = {
            "COBALT_INSTANCES": ["https://api.cobalt.tools", "https://cobalt.ducks.party"],
            "PIPED_INSTANCES": [
                "https://pipedapi.tokhmi.xyz",
                "https://pipedapi.smnz.de",
                "https://pipedapi.simpleprivacy.fr",
                "https://pipedapi.qdi.fi",
                "https://pipedapi.palveluntarjoaja.fi",
                "https://pipedapi.ggc-project.de",
                "https://pipedapi.garudalinux.org",
                "https://pipedapi.frontend.im",
                "https://pipedapi.drgns.space",
                "https://piped-api.garudalinux.org"
            ],
            "INVIDIOUS_INSTANCES": [
                "https://yewtu.be",
                "https://invidious.snopyta.org",
                "https://inv.riverside.rocks",
                "https://invidio.xamh.de",
            ]
        }
        field_name = info.field_name
        default_list = defaults.get(field_name, [])
        if v is None: return default_list
        if isinstance(v, str):
            v = v.strip()
            if not v: return default_list
            try: return json.loads(v)
            except: return [i.strip() for i in v.split(",") if i.strip()]
        if isinstance(v, list): return v
        return default_list

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
