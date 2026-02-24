import asyncio
import logging
import pickle
from pathlib import Path
from typing import Optional, Any, Union
import aiosqlite
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class CacheService:
    def __init__(self, db_path: Union[str, Path]):
        self._db_path = Path(db_path)
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Инициализация базы данных и удаление просроченных записей."""
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value BLOB,
                expires_at TIMESTAMP
            )
        """)
        await self._db.commit()
        await self._delete_expired()
        logger.info(f"Cache initialized at {self._db_path}")

    async def close(self):
        """Закрытие соединения."""
        if self._db:
            await self._db.close()
            self._db = None

    async def get(self, key: str) -> Optional[Any]:
        """Получение значения из кэша с проверкой срока годности."""
        if not self._db:
            return None
        
        try:
            async with self._lock:
                cursor = await self._db.execute(
                    "SELECT value, expires_at FROM cache WHERE key = ?", (key,)
                )
                row = await cursor.fetchone()
                if row:
                    value, expires_at = row
                    if expires_at is None or datetime.fromisoformat(expires_at) > datetime.now():
                        return pickle.loads(value)
                    else:
                        # Запись просрочена, удаляем ее
                        await self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
                        await self._db.commit()
                return None
        except Exception as e:
            logger.error(f"Cache get error for {key}: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = 3600) -> bool:
        """
        Сохранение значения в кэш с указанием времени жизни.
        :param key: Ключ
        :param value: Значение
        :param ttl: Время жизни в секундах. None или 0 означает "вечно".
        """
        if not self._db:
            return False
        
        try:
            async with self._lock:
                serialized = pickle.dumps(value)
                if ttl is not None and ttl > 0:
                    expires_at = datetime.now() + timedelta(seconds=ttl)
                    expires_at_iso = expires_at.isoformat()
                else:
                    expires_at_iso = None

                await self._db.execute(
                    "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                    (key, serialized, expires_at_iso)
                )
                await self._db.commit()
                return True
        except Exception as e:
            logger.error(f"Cache set error for {key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        """Удаление значения из кэша."""
        if not self._db:
            return False
        
        try:
            async with self._lock:
                await self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
                await self._db.commit()
                return True
        except Exception as e:
            logger.error(f"Cache delete error for {key}: {e}")
            return False

    async def clear(self) -> bool:
        """Очистка всего кэша."""
        if not self._db:
            return False
        
        try:
            async with self._lock:
                await self._db.execute("DELETE FROM cache")
                await self._db.commit()
                return True
        except Exception as e:
            logger.error(f"Cache clear error: {e}")
            return False

    async def _delete_expired(self) -> bool:
        """Удаление всех просроченных записей."""
        if not self._db:
            return False
        
        try:
            async with self._lock:
                now_iso = datetime.now().isoformat()
                await self._db.execute("DELETE FROM cache WHERE expires_at IS NOT NULL AND expires_at <= ?", (now_iso,))
                await self._db.commit()
                return True
        except Exception as e:
            logger.error(f"Cache expiration cleanup error: {e}")
            return False