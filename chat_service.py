import logging
from ai_manager import AIManager
from ai_personas import get_system_prompt

logger = logging.getLogger("chat_service")

class ChatManager:
    def __init__(self, ai_manager: AIManager):
        self.ai_manager = ai_manager
        self.histories = {} 
        self.modes = {}

    async def get_response(self, chat_id: int, text: str, user_name: str) -> str:
        mode = self.modes.get(chat_id, "default")
        
        # ⚠️ ИСПРАВЛЕНО: Передаем только один аргумент (mode), чтобы не было 500 ошибки!
        system_prompt = get_system_prompt(mode)
        
        try:
            return await self.ai_manager.get_chat_response(text, system_prompt=system_prompt)
        except Exception as e:
            logger.error(f"Chat error: {e}", exc_info=True)
            return "Что-то я потеряла нить разговора... 🤯"
            
    def set_mode(self, chat_id: int, mode: str):
        logger.info(f"ChatID {chat_id} mode set to: {mode}")
        self.modes[chat_id] = mode
        
    def get_mode(self, chat_id: int) -> str:
        return self.modes.get(chat_id, "default")
