import logging
import json
import os
import time
from typing import Optional
import httpx
import google.generativeai as genai
from google.generativeai import types
from config import Settings

logger = logging.getLogger("ai_manager")

# Bulletproof newline character to avoid copy-paste syntax errors
NL = chr(10)

class AIManager:
    """
    AI Manager (OpenRouter First, Google AI Fallback) with Circuit Breaker.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.providers = []
        self.failure_tracker = {
            "OpenRouter": {"count": 0, "blocked_until": 0},
            "GoogleAI": {"count": 0, "blocked_until": 0}
        }

        gemini_key = os.getenv("GEMINI_API_KEY") or getattr(self.settings, 'GOOGLE_API_KEY', '') or os.getenv("GOOGLE_API_KEY")
        if gemini_key:
            try:
                genai.configure(api_key=gemini_key)
                genai.GenerativeModel('gemini-pro')
                self.providers.append("GoogleAI")
                logger.info("Google AI provider configured (as fallback).")
            except Exception as e:
                logger.error(f"Google AI connection error: {e}")

        if self.settings.OPENROUTER_API_KEY:
            self.openrouter_client = httpx.AsyncClient()
            self.providers.append("OpenRouter")
            logger.info("OpenRouter provider configured (as primary).")

        if self.providers:
            logger.info("AI successfully connected (Primary: OpenRouter, Fallback: Google AI)")
        else:
            logger.error("ALL KEYS NOT FOUND! Bot running without AI.")

    def _is_blocked(self, provider: str) -> bool:
        tracker = self.failure_tracker.get(provider)
        if tracker and time.time() < tracker["blocked_until"]:
            return True
        return False

    def _record_failure(self, provider: str):
        tracker = self.failure_tracker.get(provider)
        if tracker:
            tracker["count"] += 1
            if tracker["count"] >= 3:
                logger.warning(f"Blocking provider {provider} for 5 minutes.")
                tracker["blocked_until"] = time.time() + 300
                tracker["count"] = 0

    def _clear_failure(self, provider: str):
        tracker = self.failure_tracker.get(provider)
        if tracker:
            tracker["count"] = 0
            tracker["blocked_until"] = 0

    async def _get_best_free_model(self) -> str:
        try:
            logger.info("Fetching latest free models from OpenRouter...")
            response = await self.openrouter_client.get("https://openrouter.ai/api/v1/models", timeout=10)
            data = response.json()
            free_models = [m['id'] for m in data['data'] if m.get('pricing', {}).get('prompt') == '0']
            if free_models:
                logger.info(f"Found free model: {free_models[0]}")
                return free_models[0]
        except Exception as e:
            logger.error(f"Failed to fetch free models: {e}")
        return "google/gemma-3-4b-it:free"

    async def analyze_message(self, text: str) -> dict:
        prompt = f"""Analyze this user message for a Telegram music bot.
Message: "{text}"

You MUST classify the intent strictly based on these rules:

1. intent: "radio"
- The user wants a CONTINUOUS STREAM of music.
- Keywords: "послушаем", "врубай", "радио", "волна", "микс", "плейлист", "настроение", "вайб", "поставь что-нибудь", "давай".

2. intent: "search"
- The user wants ONE SPECIFIC SONG.
- Keywords: "найди", "включи песню", "скачай".

3. intent: "chat"
- The user is talking, asking questions, greeting.

Return ONLY a valid JSON object:
{{"intent": "radio"|"search"|"chat", "query": "extracted search term or null"}}
"""

        if "OpenRouter" in self.providers:
            res = await self._call_openrouter_for_json(prompt)
            if res: return res

        if "GoogleAI" in self.providers:
            res = await self._call_flash_for_json(prompt)
            if res: return res
            
        return self._regex_fallback(text)

    async def _call_flash_for_json(self, prompt: str) -> Optional[dict]:
        if self._is_blocked("GoogleAI"): return None
        try:
            logger.warning("Falling back to Flash for JSON analysis")
            model = genai.GenerativeModel("gemini-1.5-flash-latest")
            response = await model.generate_content_async(
                prompt,
                generation_config=types.GenerationConfig(temperature=0.9, response_mime_type="application/json")
            )
            self._clear_failure("GoogleAI")
            return self._parse_json(response.text)
        except Exception as e:
            logger.error(f"Flash API error (JSON): {e}")
            self._record_failure("GoogleAI")
            return None

    async def _call_openrouter_for_json(self, prompt: str) -> Optional[dict]:
        if not self.settings.OPENROUTER_API_KEY: return None
        model = await self._get_best_free_model()
        logger.info(f"Trying OpenRouter for JSON analysis using {model}...")
        try:
            response = await self.openrouter_client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.OPENROUTER_API_KEY}",
                    "HTTP-Referer": "https://github.com/coolmag/oldbot26",
                    "X-Title": "Aurora AI DJ"
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"}
                },
                timeout=20
            )
            response.raise_for_status()
            data = response.json()
            return self._parse_json(data["choices"][0]["message"]["content"])
        except Exception as e:
            logger.error(f"OpenRouter JSON analysis failed: {e}")
        return None

    def _regex_fallback(self, text: str) -> dict:
        logger.warning("AI analysis failed. Using Regex Fallback.")
        text_lower = text.lower()
        
        chat_keywords = ['привет', 'как дела', 'что делаешь', 'аврора', 'бот', 'кто ты', 'на связи']
        if any(k in text_lower for k in chat_keywords) and len(text.split()) < 6:
            return {"intent": "chat", "query": None}
            
        radio_keywords = ['радио', 'волна', 'микс', 'плейлист', 'врубай', 'давай', 'послушаем', 'включи']
        if any(k in text_lower for k in radio_keywords):
            query = text
            for k in radio_keywords: 
                query = query.lower().replace(k, '')
            return {"intent": "radio", "query": query.strip() or "top hits"}
            
        return {"intent": "search", "query": text}

    async def get_chat_response(self, prompt: str, system_prompt: str = "") -> str:
        full_prompt = system_prompt + NL + "User: " + prompt
        
        if "OpenRouter" in self.providers and not self._is_blocked("OpenRouter"):
            try:
                logger.info("Trying OpenRouter for Chat...")
                response = await self._call_openrouter(full_prompt)
                if response:
                    return response
            except Exception as e:
                logger.error(f"OpenRouter (Primary) failed: {e}")

        if "GoogleAI" in self.providers and not self._is_blocked("GoogleAI"):
            try:
                logger.warning("Falling back to Flash for Chat")
                model = genai.GenerativeModel("gemini-1.5-flash-latest")
                response = await model.generate_content_async(
                    full_prompt,
                    generation_config=types.GenerationConfig(temperature=0.9)
                )
                self._clear_failure("GoogleAI")
                logger.info("Flash (Chat) responded.")
                return response.text
            except Exception as e:
                logger.error(f"Flash fallback failed: {e}")
                self._record_failure("GoogleAI")

        return "Извини, мои нейромодули обесточены. Проверь API-ключ!"

    async def _call_openrouter(self, full_prompt: str) -> Optional[str]:
        if not self.settings.OPENROUTER_API_KEY or self._is_blocked("OpenRouter"): return None
        
        try:
            response = await self.openrouter_client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.OPENROUTER_API_KEY}",
                    "HTTP-Referer": "https://github.com/coolmag/oldbot26",
                    "X-Title": "Aurora AI DJ"
                },
                json={
                    "model": "deepseek/deepseek-v3-base:free",
                    "messages": [
                        {"role": "user", "content": full_prompt}
                    ]
                },
                timeout=40
            )
            response.raise_for_status()
            data = response.json()
            logger.info("OpenRouter responded.")
            self._clear_failure("OpenRouter")
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            logger.error(f"OpenRouter HTTP Error: {e.response.status_code} - {e.response.text}")
            self._record_failure("OpenRouter")
        except Exception as e:
            logger.error(f"OpenRouter call failed: {e}")
            self._record_failure("OpenRouter")
        return None

    async def test_providers(self) -> str:
        report = ["**AI Providers Status Check:**"]

        if "GoogleAI" in self.providers:
            try:
                model = genai.GenerativeModel("gemini-1.5-flash-latest")
                response = await model.generate_content_async("test", generation_config=types.GenerationConfig(temperature=0.1))
                if response.text:
                    report.append("Google AI: OK")
                else:
                    raise Exception("Empty response received")
            except Exception as e:
                error_summary = str(e).splitlines()[0]
                report.append("Google AI: FAILED. Reason: " + error_summary)
                logger.error(f"DIAGNOSTIC: Google AI test failed: {e}")
        else:
            report.append("Google AI: SKIPPED (no key)")

        if "OpenRouter" in self.providers:
            try:
                test_prompt = "Hello"
                or_response = await self._call_openrouter(test_prompt)
                if or_response:
                    report.append("OpenRouter: OK")
                else:
                    raise Exception("Empty response.")
            except Exception as e:
                error_summary = str(e).splitlines()[0]
                report.append("OpenRouter: FAILED. Reason: " + error_summary)
                logger.error(f"DIAGNOSTIC: OpenRouter test failed: {e}")
        else:
            report.append("OpenRouter: SKIPPED (no key)")

        return NL.join(report)

    async def transcribe_voice(self, voice_bytes: bytearray) -> Optional[str]:
        if "GoogleAI" not in self.providers:
            logger.warning("Voice transcription skipped: Google AI provider not configured.")
            return None
        try:
            model = genai.GenerativeModel('gemini-1.5-flash-latest')
            response = await model.generate_content_async(
                [
                    types.Part.from_bytes(data=bytes(voice_bytes), mime_type='audio/ogg'),
                    "Transcribe this voice message to text. Output ONLY the text."
                ]
            )
            return response.text.strip()
        except Exception as e:
            logger.error(f"Voice processing failed: {e}")
            return None

    def _parse_json(self, text: str) -> Optional[dict]:
        try:
            start = text.find('{')
            end = text.rfind('}') + 1
            if start == -1 or end == 0: return None
            return json.loads(text[start:end])
        except (json.JSONDecodeError, TypeError): return None
