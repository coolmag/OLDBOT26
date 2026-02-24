from dataclasses import dataclass, field
from typing import Optional, List, Union
from pathlib import Path
from enum import Enum

# 👇 ВОТ ОН, ПОТЕРЯННЫЙ SOURCE
class Source(str, Enum):
    YOUTUBE = "youtube"
    SPOTIFY = "spotify"
    SOUNDCLOUD = "soundcloud"
    JAMENDO = "jamendo"
    YTMUSIC = "ytmusic"

@dataclass
class TrackInfo:
    identifier: str
    title: str
    duration: int
    # Поле, которое реально используется
    uploader: str = "Unknown Artist" 
    thumbnail_url: Optional[str] = None
    source: str = "youtube"
    
    # Дополнительные поля для Spotify (если нужны)
    album: Optional[str] = None
    url: Optional[str] = None

    # Алиасы для совместимости (чтобы не падало)
    @property
    def artist(self) -> str:
        return self.uploader
        
    @property
    def author(self) -> str:
        return self.uploader
        
    @classmethod
    def from_yt_info(cls, info: dict):
        # Умный парсинг артиста
        artist = info.get('uploader') or info.get('artist') or info.get('creator') or "Unknown"
        
        return cls(
            identifier=info.get('id', ''),
            title=info.get('title', 'Unknown'),
            uploader=artist,
            duration=int(info.get('duration', 0)),
            thumbnail_url=info.get('thumbnail')
        )

@dataclass
class DownloadResult:
    success: bool
    file_path: Optional[Union[Path, str]] = None
    is_url: bool = False  # Flag to indicate if file_path is a URL
    file_id: Optional[str] = None  # Telegram file_id
    track_info: Optional[TrackInfo] = None
    error_message: Optional[str] = None