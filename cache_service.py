import logging
import json
from typing import Any, Optional
import redis.asyncio as redis
from config import get_settings

logger = logging.getLogger(__name__)

class CacheService:
    def __init__(self):
        self.settings = get_settings()
        self.redis_client = None
        self._in_memory_cache = {}
        
        if self.settings.REDIS_URL:
            try:
                self.redis_client = redis.from_url(self.settings.REDIS_URL, decode_responses=True)
                logger.info("✅ Redis client initialized successfully.")
            except Exception as e:
                logger.error(f"❌ Failed to initialize Redis: {e}. Falling back to in-memory cache.")
        else:
            logger.info("ℹ️ Redis URL not provided. Using in-memory cache.")

    async def set(self, key: str, value: Any, ttl: Optional[int] = 3600):
        if self.redis_client:
            try:
                await self.redis_client.set(key, json.dumps(value), ex=ttl)
            except Exception as e:
                logger.error(f"Redis set error: {e}")
                self._in_memory_cache[key] = value
        else:
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

# Глобальный экземпляр для использования в других модулях
cache_service = CacheService()
