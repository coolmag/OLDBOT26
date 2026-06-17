import logging
import json
from typing import Any, Optional
import redis.asyncio as redis
from config import get_settings

logger = logging.getLogger(__name__)

class CacheService:
    def __init__(self, db_path: Optional[str] = None):
        self.settings = get_settings()
        self.redis_client = None
        self._in_memory_cache = {}
        self._max_cache_size = 500 # Лимит записей
        
        if self.settings.REDIS_URL:
            try:
                self.redis_client = redis.from_url(self.settings.REDIS_URL, decode_responses=True)
                logger.info("✅ Redis client initialized successfully.")
            except Exception as e:
                logger.error(f"❌ Failed to initialize Redis: {e}. Falling back to in-memory cache.")
        else:
            logger.info("ℹ️ Redis URL not provided. Using in-memory cache.")

    async def initialize(self):
        pass

    async def close(self):
        pass

    async def set(self, key: str, value: Any, ttl: Optional[int] = 3600):
        if self.redis_client:
            try:
                await self.redis_client.set(key, json.dumps(value), ex=ttl)
            except Exception as e:
                logger.error(f"Redis set error: {e}")
                self._add_to_memory_cache(key, value)
        else:
            self._add_to_memory_cache(key, value)

    def _add_to_memory_cache(self, key: str, value: Any):
        if len(self._in_memory_cache) >= self._max_cache_size:
            oldest_key = next(iter(self._in_memory_cache))
            self._in_memory_cache.pop(oldest_key)
        self._in_memory_cache[key] = value

    async def get(self, key: str) -> Optional[Any]:
        if self.redis_client:
            try:
                val = await self.redis_client.get(key)
                return json.loads(val) if val else None
            except Exception as e:
                logger.error(f"Redis get error: {e}")
                return self._in_memory_cache.get(key)
        else:
            return self._in_memory_cache.get(key)

    async def delete(self, key: str):
        if self.redis_client:
            try:
                await self.redis_client.delete(key)
            except Exception as e:
                logger.error(f"Redis delete error: {e}")
        self._in_memory_cache.pop(key, None)

    async def hincr(self, name: str, key: str, amount: int = 1):
        if self.redis_client:
            try:
                await self.redis_client.hincrby(name, key, amount)
            except Exception as e:
                logger.error(f"Redis hincr error: {e}")
                self._fallback_hincr(name, key, amount)
        else:
            self._fallback_hincr(name, key, amount)

    def _fallback_hincr(self, name: str, key: str, amount: int):
        if name not in self._in_memory_cache: self._in_memory_cache[name] = {}
        if not isinstance(self._in_memory_cache[name], dict): self._in_memory_cache[name] = {}
        val = self._in_memory_cache[name].get(key, 0)
        self._in_memory_cache[name][key] = val + amount

    async def hgetall(self, name: str) -> dict:
        if self.redis_client:
            try:
                return await self.redis_client.hgetall(name)
            except Exception as e:
                logger.error(f"Redis hgetall error: {e}")
                return self._in_memory_cache.get(name, {})
        else:
            return self._in_memory_cache.get(name, {})

    # НОВЫЕ МЕТОДЫ ДЛЯ АНАЛИТИКИ
    async def record_failure(self, track_id: str):
        await self.hincr("fail_stats", track_id, 1)

    async def get_failure_stats(self) -> dict:
        return await self.hgetall("fail_stats")

# Глобальный экземпляр для использования в других модулях
cache_service = CacheService()
