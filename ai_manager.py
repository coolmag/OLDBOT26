import logging
import json
from typing import Optional
import httpx
from google import genai
from config import Settings

logger = logging.getLogger("ai_manager")

class AIManager:
    """
    🧠 AI Manager (DI Ready).
    Accepts settings via constructor.
    """
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.providers = []
        
        if self.settings.OPENROUTER_API_KEY:
            self.providers.append("OpenRouter")
            
        if self.settings.GOOGLE_API_KEY:
            try:
                self.gemini_client = genai.Client(api_key=self.settings.GOOGLE_API_KEY)
                self.providers.append("Gemini")
                logger.info("✅ Gemini Client configured successfully.")
            except Exception as e:
                logger.error(f"Failed to configure Gemini Client: {e}")

    async def analyze_message(self, text: str) -> dict:
        prompt = f"""
        Analyze this user message for a music bot.
        Message: "{text}"
        
        Return ONLY a JSON object (no markdown, no preamble) with:
        1. "intent": can be "radio", "search", or "chat".
        2. "query": a clean search term or genre for "search" and "radio", or null for "chat".
        Example for radio: {{"intent": "radio", "query": "90s rock"}}
        Example for search: {{"intent": "search", "query": "Daft Punk - Around the world"}}
        Example for chat: {{"intent": "chat", "query": null}}
        """

        if "OpenRouter" in self.providers:
            res = await self._call_openrouter_for_json(prompt)
            if res: return res

        if "Gemini" in self.providers:
            res = await self._call_gemini_for_json(prompt)
            if res: return res
            
        return self._regex_fallback(text)

    async def _call_gemini_for_json(self, prompt: str) -> Optional[dict]:
        try:
            response = self.gemini_client.models.generate_content(
                model="gemini-2.0-flash", 
                contents=prompt
            )
            logger.info("🧠 Gemini (JSON) responded.")
            return self._parse_json(response.text)
        except Exception as e:
            logger.error(f"❌ Gemini API error (JSON): {e}")
            return None

    async def _call_openrouter_for_json(self, prompt: str) -> Optional[dict]:
        # Prioritize free models
        free_models = ["google/gemini-2.0-flash-exp:free", "meta-llama/llama-3.2-3b-instruct:free"]
        headers = {
            "Authorization": f"Bearer {self.settings.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://aurora-player.cloud" # Mock referer
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            for model in free_models:
                try:
                    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "response_format": {"type": "json_object"}}
                    resp = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
                    if resp.status_code == 200:
                        logger.info(f"🧠 OpenRouter/{model} (JSON) responded.")
                        return self._parse_json(resp.json()['choices'][0]['message']['content'])
                except Exception:
                    continue # Try next model
        logger.warning(" OpenRouter failed for all free models (JSON).")
        return None

    def _regex_fallback(self, text: str) -> dict:
        logger.warning("⚠️ AI analysis failed. Using Regex Fallback.")
        text_lower = text.lower()
        radio_keywords = ['радио', 'волна', 'микс', 'плейлист', 'radio', 'wave', 'mix', 'playlist', 'play', 'играй', 'включи']
        
        # More specific check for radio
        if any(k in text_lower for k in radio_keywords):
            # Remove keywords to find the genre
            query = text
            for k in radio_keywords:
                query = query.lower().replace(k, '')
            query = query.strip()
            # If after cleaning nothing is left, use a default genre
            return {"intent": "radio", "query": query or "top hits"}
            
        # Default to search if no radio keywords
        return {"intent": "search", "query": text}

    async def get_chat_response(self, prompt: str, system_prompt: str = "") -> str:
        """Generates a conversational response."""
        
        # 1. Try OpenRouter
        if "OpenRouter" in self.providers:
            try:
                headers = {
                    "Authorization": f"Bearer {self.settings.OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://aurora-player.cloud"
                }
                payload = {
                    "model": "google/gemini-2.0-flash-exp:free",
                    "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]
                }
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
                    if resp.status_code == 200:
                        logger.info("💬 OpenRouter (Chat) responded.")
                        return resp.json()['choices'][0]['message']['content']
            except Exception as e:
                logger.warning(f"OpenRouter chat failed: {e}")

        # 2. Fallback to Gemini
        if "Gemini" in self.providers:
            try:
                full_prompt = f"{system_prompt}\n\nUser: {prompt}"
                response = self.gemini_client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=full_prompt
                )
                logger.info("💬 Gemini (Chat) responded.")
                return response.text
            except Exception as e:
                logger.error(f"❌ Gemini chat failed: {e}")
            
        return "Извини, я сейчас немного занят музыкой, давай поболтаем позже! 🎧"

    def _parse_json(self, text: str) -> Optional[dict]:
        try:
            # Most robust way to find JSON within a string
            start = text.find('{')
            end = text.rfind('}') + 1
            if start == -1 or end == 0: return None
            
            json_str = text[start:end]
            return json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            logger.error(f"Failed to parse JSON from AI response: '{text}'")
            return None
