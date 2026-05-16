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
    🧠 AI Manager (Hybrid: Flash for JSON routing, Gemma 3 for Chat/Jokes).
    """
    def __init__(self, settings: Settings):
        self.settings = settings
        self.providers = []
        
        # Setup Google AI
        gemini_key = os.getenv("GEMINI_API_KEY") or getattr(self.settings, 'GOOGLE_API_KEY', '') or os.getenv("GOOGLE_API_KEY")
        if gemini_key:
            try:
                self.gemini_client = genai.Client(api_key=gemini_key)
                self.providers.append("GoogleAI")
                logger.info("✅ Google AI provider configured.")
            except Exception as e:
                logger.error(f"❌ Google AI connection error: {e}")

        # Setup OpenRouter
        if self.settings.OPENROUTER_API_KEY:
            self.openrouter_client = httpx.AsyncClient()
            self.providers.append("OpenRouter")
            logger.info("✅ OpenRouter provider configured.")

        if self.providers:
            logger.info(f"✅ ИИ успешно подключен (Мозг: Gemma 4 e2b, Логика/Уши: Gemini Flash, Резерв: OpenRouter)")
        else:
            logger.error("❌ ВСЕ КЛЮЧИ НЕ НАЙДЕНЫ! Бот работает в режиме без ИИ.")

    async def analyze_message(self, text: str) -> dict:
        prompt = f"""Analyze this user message for a Telegram music bot.
        Message: "{text}"
        
        You MUST classify the intent strictly based on these rules:
        
        1. intent: "radio"
        - The user wants a CONTINUOUS STREAM of music.
        - Keywords: "послушаем", "врубай", "радио", "волна", "микс", "плейлист", "настроение", "вайб", "поставь что-нибудь", "давай".
        - Example 1: "послушаем линкин парк" -> intent: "radio", query: "linkin park"
        - Example 2: "врубай советский грув" -> intent: "radio", query: "советский грув"

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
            # 🔥 ИСПОЛЬЗУЕМ FLASH ДЛЯ 100% НАДЕЖНОГО JSON
            res = await self._call_flash_for_json(prompt)
            if res: return res
            
        return self._regex_fallback(text)

    async def _call_flash_for_json(self, prompt: str) -> Optional[dict]:
        try:
            # ⚠️ Gemini 2.5 Flash идеально парсит JSON
            response = self.gemini_client.models.generate_content(
                model="gemini-2.5-flash", 
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0.1)
            )
            return self._parse_json(response.text)
        except Exception as e:
            logger.error(f"❌ Flash API error (JSON): {e}")
            return None

    def _regex_fallback(self, text: str) -> dict:
        logger.warning("⚠️ AI analysis failed. Using Regex Fallback.")
        text_lower = text.lower()
        
        chat_keywords = ['привет', 'как дела', 'что делаешь', 'аврора', 'бот', 'кто ты', 'на связи']
        if any(k in text_lower for k in chat_keywords) and len(text.split()) < 6:
            return {"intent": "chat", "query": None}
            
        # ⚠️ ДОБАВИЛИ ВСЕ СЛОВА ДЛЯ РАДИО
        radio_keywords = ['радио', 'волна', 'микс', 'плейлист', 'врубай', 'давай', 'послушаем', 'включи']
        if any(k in text_lower for k in radio_keywords):
            query = text
            for k in radio_keywords: query = query.lower().replace(k, '')
            return {"intent": "radio", "query": query.strip() or "top hits"}
            
        return {"intent": "search", "query": text}

    async def get_chat_response(self, prompt: str, system_prompt: str = "") -> str:
        full_prompt = f"{system_prompt}\n\nUser: {prompt}"
        
        # --- Level 1: Google AI (Primary) ---
        if "GoogleAI" in self.providers:
            for attempt in range(2):
                try:
                    response = self.gemini_client.models.generate_content(
                        model="gemma-4-e2b",
                        contents=full_prompt,
                        config=types.GenerateContentConfig(temperature=0.9)
                    )
                    logger.info("💬 Gemma 4 e2b (Chat) responded.")
                    return response.text
                except Exception as e:
                    logger.error(f"❌ Gemma 4 attempt {attempt+1} failed: {e}")
                    if "RESOURCE_EXHAUSTED" in str(e): break # No point retrying on quota errors
                    import asyncio
                    await asyncio.sleep(1)
                    
            # --- Level 2: Google AI (Fallback) ---
            try:
                logger.warning("🔄 Falling back to Gemini Flash for Chat")
                response = self.gemini_client.models.generate_content(
                    model="gemini-2.5-flash", 
                    contents=full_prompt, 
                    config=types.GenerateContentConfig(temperature=0.9)
                )
                return response.text
            except Exception as e:
                logger.error(f"❌ Flash fallback failed: {e}")

        # --- Level 3: OpenRouter (Final Fallback) ---
        if "OpenRouter" in self.providers:
            try:
                logger.warning("🔄 Falling back to OpenRouter for Chat")
                return await self._call_openrouter(full_prompt)
            except Exception as e:
                logger.error(f"❌ OpenRouter fallback failed: {e}")

        return "Извини, мои нейромодули обесточены. Проверь API-ключ! 🔌"

    async def _call_openrouter(self, full_prompt: str) -> Optional[str]:
        if not self.settings.OPENROUTER_API_KEY: return None
        
        try:
            response = await self.openrouter_client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.OPENROUTER_API_KEY}",
                    "HTTP-Referer": "https://github.com/coolmag/oldbot26",
                    "X-Title": "Aurora AI DJ"
                },
                json={
                    "model": "mistralai/mistral-7b-instruct:free",
                    "messages": [
                        {"role": "user", "content": full_prompt}
                    ]
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.info("💬 OpenRouter (Mistral 7B) responded.")
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            logger.error(f"OpenRouter HTTP Error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logger.error(f"OpenRouter call failed: {e}")
        return None

    async def transcribe_voice(self, voice_bytes: bytearray) -> Optional[str]:
        if "GoogleAI" not in self.providers:
            return None
        try:
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
            return None

    def _parse_json(self, text: str) -> Optional[dict]:
        try:
            start = text.find('{')
            end = text.rfind('}') + 1
            if start == -1 or end == 0: return None
            return json.loads(text[start:end])
        except (json.JSONDecodeError, TypeError): return None
