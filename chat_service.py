import logging
from ai_manager import AIManager
from ai_personas import get_system_prompt
from cache_service import CacheService # 🟢 Добавили

logger = logging.getLogger("chat_service")

class ChatManager:
    def __init__(self, ai_manager: AIManager, cache: CacheService): # 🟢 Прокидываем cache
        self.ai_manager = ai_manager
        self.cache = cache
        self.modes = {}

    async def get_response(self, chat_id: int, text: str, user_name: str) -> str:
        mode = await self.get_mode(chat_id) # 🟢 Теперь асинхронно
        system_prompt = get_system_prompt(mode)
        try:
            return await self.ai_manager.get_chat_response(text, system_prompt=system_prompt)
        except Exception as e:
            logger.error(f"Chat error: {e}", exc_info=True)
            return "Что-то я потеряла нить разговора... 🤯"
            
    async def set_mode(self, chat_id: int, mode: str):
        logger.info(f"ChatID {chat_id} mode set to: {mode}")
        self.modes[chat_id] = mode
        await self.cache.set(f"mode_{chat_id}", mode, ttl=0) # 🟢 Пишем в БД навсегда
        
    async def get_mode(self, chat_id: int) -> str:
        if chat_id in self.modes:
            return self.modes[chat_id]
        cached_mode = await self.cache.get(f"mode_{chat_id}") # 🟢 Читаем из БД
        mode = cached_mode or "default"
        self.modes[chat_id] = mode
        return mode
