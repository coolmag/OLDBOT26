import logging
import json
import os
from typing import Optional
import httpx
from google import genai
from config import Settings

logger = logging.getLogger("ai_manager")

class AIManager:
    """
    🧠 AI Manager (Умный гибрид: Flash для логики, Gemma 3 для общения).
    """
    def __init__(self, settings: Settings):
        self.settings = settings
        self.providers = []
        
        # ⚠️ АГРЕССИВНЫЙ ПОИСК КЛЮЧЕЙ (Берем из переменных Railway напрямую!)
        gemini_key = os.getenv("GEMINI_API_KEY") or getattr(self.settings, 'GOOGLE_API_KEY', '') or os.getenv("GOOGLE_API_KEY")
        openrouter_key = os.getenv("OPENROUTER_API_KEY") or getattr(self.settings, 'OPENROUTER_API_KEY', '')
        
        if openrouter_key:
            self.settings.OPENROUTER_API_KEY = openrouter_key
            self.providers.append("OpenRouter")
            
        if gemini_key:
            try:
                self.gemini_client = genai.Client(api_key=gemini_key)
                self.providers.append("Gemini")
                logger.info("✅ ИИ успешно подключен (Ключ найден!)")
            except Exception as e:
                logger.error(f"❌ Ошибка подключения ИИ: {e}")
                
        if not self.providers:
            logger.error("❌ КЛЮЧИ НЕ НАЙДЕНЫ! Бот работает в режиме без ИИ.")

    async def analyze_message(self, text: str) -> dict:
        prompt = f"""
        Analyze this user message for a Telegram music bot.
        Message: "{text}"
        
        Intent rules:
        - "radio": user wants to listen to a stream, genre, mood, or random music.
        - "search": user wants a specific song or artist.
        - "chat": user is just greeting, asking questions, or making conversation.
        
        Return ONLY a valid JSON object:
        {{"intent": "radio"|"search"|"chat", "query": "extracted search term or null"}}
        """

        if "Gemini" in self.providers:
            res = await self._call_gemini_for_json(prompt)
            if res: return res

        if "OpenRouter" in self.providers:
            res = await self._call_openrouter_for_json(prompt)
            if res: return res
            
        return self._regex_fallback(text)

    async def _call_gemini_for_json(self, prompt: str) -> Optional[dict]:
        try:
            # Для генерации JSON используем Flash (он лучше следует синтаксису)
            response = self.gemini_client.models.generate_content(
                model="gemini-2.0-flash", 
                contents=prompt
            )
            return self._parse_json(response.text)
        except Exception as e:
            logger.error(f"❌ AI API error (JSON): {e}")
            return None

    async def _call_openrouter_for_json(self, prompt: str) -> Optional[dict]:
        free_models = ["google/gemma-3-27b-it:free", "google/gemini-2.0-flash-exp:free"]
        headers = {
            "Authorization": f"Bearer {self.settings.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://aurora-player.cloud"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            for model in free_models:
                try:
                    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "response_format": {"type": "json_object"}}
                    resp = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
                    if resp.status_code == 200:
                        return self._parse_json(resp.json()['choices'][0]['message']['content'])
                except Exception: continue
        return None

    def _regex_fallback(self, text: str) -> dict:
        logger.warning("⚠️ AI analysis failed. Using Regex Fallback.")
        text_lower = text.lower()
        chat_keywords = ['привет', 'как дела', 'что делаешь', 'аврора', 'бот', 'кто ты', 'на связи']
        if any(k in text_lower for k in chat_keywords) and len(text.split()) < 6:
            return {"intent": "chat", "query": None}
            
        radio_keywords = ['радио', 'волна', 'микс', 'плейлист', 'radio', 'wave', 'mix', 'playlist', 'включи', 'поставь']
        if any(k in text_lower for k in radio_keywords):
            query = text
            for k in radio_keywords: query = query.lower().replace(k, '')
            return {"intent": "radio", "query": query.strip() or "top hits"}
            
        return {"intent": "search", "query": text}

    async def get_chat_response(self, prompt: str, system_prompt: str = "") -> str:
        # 1. ОСНОВНОЙ ИИ - GEMMA 3 (С огромными лимитами)
        if "Gemini" in self.providers:
            try:
                full_prompt = f"{system_prompt}\n\nUser: {prompt}"
                response = self.gemini_client.models.generate_content(
                    model="gemma-3-27b-it", # 🔥 ВОТ ОНА, GEMMA 3!
                    contents=full_prompt
                )
                logger.info("💬 Gemma 3 (Chat) responded.")
                return response.text
            except Exception as e:
                logger.error(f"❌ Gemma 3 chat failed (trying fallback): {e}")
                # Если Google обновит названия моделей, падаем на Flash
                try:
                    response = self.gemini_client.models.generate_content(model="gemini-2.0-flash", contents=full_prompt)
                    return response.text
                except: pass
                
        # 2. Резерв на OpenRouter
        if "OpenRouter" in self.providers:
            try:
                headers = {"Authorization": f"Bearer {self.settings.OPENROUTER_API_KEY}", "Content-Type": "application/json", "HTTP-Referer": "https://aurora-player.cloud"}
                payload = {"model": "google/gemma-3-27b-it:free", "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]}
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
                    if resp.status_code == 200:
                        logger.info("💬 OpenRouter (Chat) responded.")
                        return resp.json()['choices'][0]['message']['content']
            except Exception as e: logger.warning(f"OpenRouter chat failed: {e}")

        return "Извини, мои нейромодули обесточены. Проверь API-ключ! 🔌"

    def _parse_json(self, text: str) -> Optional[dict]:
        try:
            start = text.find('{')
            end = text.rfind('}') + 1
            if start == -1 or end == 0: return None
            return json.loads(text[start:end])
        except (json.JSONDecodeError, TypeError): return None
