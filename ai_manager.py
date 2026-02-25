import logging
import json
import os
from typing import Optional
import httpx
from google import genai
from google.genai import types
from config import Settings

logger = logging.getLogger("ai_manager")

class AIManager:
    """
    🧠 AI Manager (Gemma 3 Core + Gemini Flash Ears).
    """
    def __init__(self, settings: Settings):
        self.settings = settings
        self.providers = []
        
        gemini_key = os.getenv("GEMINI_API_KEY") or getattr(self.settings, 'GOOGLE_API_KEY', '') or os.getenv("GOOGLE_API_KEY")
        
        if gemini_key:
            try:
                self.gemini_client = genai.Client(api_key=gemini_key)
                self.providers.append("GoogleAI")
                logger.info("✅ ИИ успешно подключен (Мозг: Gemma 3, Уши: Gemini Flash)")
            except Exception as e:
                logger.error(f"❌ Ошибка подключения ИИ: {e}")
                
        if not self.providers:
            logger.error("❌ КЛЮЧИ НЕ НАЙДЕНЫ! Бот работает в режиме без ИИ.")

    async def analyze_message(self, text: str) -> dict:
        prompt = f"""Analyze this user message for a Telegram music bot.
        Message: "{text}"
        
        You MUST classify the intent strictly based on these rules:
        
        1. intent: "radio"
        - The user wants a CONTINUOUS STREAM of music.
        - Keywords: "послушаем", "врубай", "радио", "волна", "микс", "плейлист", "настроение", "вайб", "поставь что-нибудь".

        2. intent: "search"
        - The user wants ONE SPECIFIC SONG.
        - Keywords: "найди", "включи песню", "скачай".
        - Example: "Сектор газа лирика | для Сани" -> intent: "search", query: "Сектор газа лирика | для Сани"

        3. intent: "chat"
        - The user is talking, asking questions, greeting.
        - Example: "как дела?".

        Return ONLY a valid JSON object:
        {{"intent": "radio"|"search"|"chat", "query": "extracted search term or null"}}
        """

        if "GoogleAI" in self.providers:
            res = await self._call_gemma_for_json(prompt)
            if res: return res
            
        return self._regex_fallback(text)

    async def _call_gemma_for_json(self, prompt: str) -> Optional[dict]:
        try:
            # Мозг: Gemma 3
            response = self.gemini_client.models.generate_content(
                model="gemma-3-27b-it", 
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.1)
            )
            return self._parse_json(response.text)
        except Exception as e:
            logger.error(f"❌ Gemma API error (JSON): {e}")
            return None

    def _regex_fallback(self, text: str) -> dict:
        logger.warning("⚠️ AI analysis failed. Using Regex Fallback.")
        text_lower = text.lower()
        
        chat_keywords = ['привет', 'как дела', 'что делаешь', 'аврора', 'бот', 'кто ты', 'на связи']
        if any(k in text_lower for k in chat_keywords) and len(text.split()) < 6:
            return {"intent": "chat", "query": None}
            
        radio_keywords = ['радио', 'волна', 'микс', 'плейлист', 'врубай', 'давай', 'послушаем']
        if any(k in text_lower for k in radio_keywords):
            query = text
            for k in radio_keywords: query = query.lower().replace(k, '')
            return {"intent": "radio", "query": query.strip() or "top hits"}
            
        return {"intent": "search", "query": text}

    async def get_chat_response(self, prompt: str, system_prompt: str = "") -> str:
        if "GoogleAI" in self.providers:
            try:
                full_prompt = f"{system_prompt}\n\nUser: {prompt}"
                # Чат и шутки: Максимально креативная Gemma 3
                response = self.gemini_client.models.generate_content(
                    model="gemma-3-27b-it",
                    contents=full_prompt,
                    config=types.GenerateContentConfig(temperature=0.9)
                )
                logger.info("💬 Gemma 3 27B (Chat) responded.")
                return response.text
            except Exception as e:
                logger.error(f"❌ Gemma 3 chat failed: {e}")
                
        return "Извини, мои нейромодули обесточены. Проверь API-ключ! 🔌"

    # 🔥 ИСПРАВЛЕНИЕ ЗДЕСЬ: ВОЗВРАЩАЕМ GEMINI FLASH ТОЛЬКО ДЛЯ СЛУХА
    async def transcribe_voice(self, voice_bytes: bytearray) -> Optional[str]:
        if "GoogleAI" not in self.providers:
            return None
        try:
            # ⚠️ GEMMA 3 НЕ УМЕЕТ СЛУШАТЬ АУДИО! Используем 2.5-flash только как микрофон.
            response = self.gemini_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[
                    types.Part.from_bytes(data=bytes(voice_bytes), mime_type='audio/ogg'),
                    "Транскрибируй это голосовое сообщение в текст. Выведи ТОЛЬКО текст, без кавычек."
                ]
            )
            return response.text.strip()
        except Exception as e:
            logger.error(f"❌ Voice processing failed: {e}")
            # Резерв на случай, если 2.5-flash заблокирован, пробуем 2.0-flash
            try:
                fallback = self.gemini_client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=[
                        types.Part.from_bytes(data=bytes(voice_bytes), mime_type='audio/ogg'),
                        "Транскрибируй это."
                    ]
                )
                return fallback.text.strip()
            except:
                return None

    def _parse_json(self, text: str) -> Optional[dict]:
        try:
            start = text.find('{')
            end = text.rfind('}') + 1
            if start == -1 or end == 0: return None
            return json.loads(text[start:end])
        except (json.JSONDecodeError, TypeError): return None
