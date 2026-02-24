# Version: 4.0 - Masterpiece 2026
import logging
import asyncio
from contextlib import asynccontextmanager
import shutil
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, BotCommand
from telegram.ext import Application

from config import get_settings
from logging_setup import setup_logging
from radio import RadioManager
from youtube import YouTubeDownloader
from spotify import SpotifyService
from handlers import setup_handlers
from cache_service import CacheService
from ai_manager import AIManager
from chat_service import ChatManager

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    app.state.settings = settings
    
    logger.info("⚡ System Starting Up (Cobalt Engine)...")
    if shutil.which("ffmpeg"): logger.info("✅ FFmpeg detected.")
    else: logger.warning("⚠️ FFmpeg not found!")

    cache = CacheService(settings.CACHE_DB_PATH)
    await cache.initialize()
    
    ai_manager = AIManager(settings)
    chat_manager = ChatManager(ai_manager)
    
    downloader = YouTubeDownloader(settings, cache)
    spotify_service = SpotifyService(settings, downloader)
    
    builder = Application.builder().token(settings.BOT_TOKEN).read_timeout(30).write_timeout(30)
    tg_app = builder.build()
    
    # ВНИМАНИЕ: Передаем chat_manager в радио
    radio_manager = RadioManager(bot=tg_app.bot, settings=settings, downloader=downloader, chat_manager=chat_manager)
    
    tg_app.ai_manager = ai_manager
    tg_app.chat_manager = chat_manager
    tg_app.downloader = downloader
    tg_app.radio_manager = radio_manager
    tg_app.spotify_service = spotify_service
    tg_app.settings = settings
    tg_app.cache = cache
    
    setup_handlers(tg_app)
    
    commands = [BotCommand("radio", "🎲 Случайная волна"), BotCommand("play", "🔎 Найти трек"), BotCommand("skip", "⏭ Следующий трек"), BotCommand("stop", "🛑 Остановить"), BotCommand("admin", "⚙️ Настройки")]
    await tg_app.bot.set_my_commands(commands)
    
    await tg_app.initialize()
    await tg_app.start()
    
    if settings.WEBHOOK_URL:
        await tg_app.bot.set_webhook(url=settings.WEBHOOK_URL)
        logger.info(f"🔗 Webhook set to: {settings.WEBHOOK_URL}")
    
    app.state.tg_app = tg_app
    app.state.chat_manager = chat_manager
    app.state.downloader = downloader
    
    yield
    
    logger.info("🔻 System Shutting Down...")
    await radio_manager.stop_all()
    await tg_app.stop()
    await tg_app.shutdown()
    await cache.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/api/health")
async def health_check():
    """Railway Health Check Endpoint"""
    return {"status": "ok", "engine": "Aurora v3.1"}

@app.post("/telegram")
async def telegram_webhook(request: Request):
    tg_app = request.app.state.tg_app
    try:
        update = Update.de_json(await request.json(), tg_app.bot)
        await tg_app.process_update(update)
    except Exception as e:
        logger.error(f"Webhook processing error: {e}", exc_info=True)
    return {"ok": True}

@app.get("/api/ai/dj")
async def api_ai_dj(prompt: str, request: Request):
    """Эндпоинт для ИИ-диджея в веб-плеере"""
    chat_manager = request.app.state.chat_manager
    ai_manager = request.app.state.tg_app.ai_manager
    downloader = request.app.state.downloader
    
    # 1. Получаем разговорный ответ ИИ (с шутками и стилем персоны)
    # Используем chat_id = 0 для веб-плеера (чтобы бралась дефолтная персона или последняя установленная)
    ai_message = await chat_manager.get_response(0, prompt, "Слушатель")
    
    # 2. Анализируем, что именно нужно найти
    analysis = await ai_manager.analyze_message(prompt)
    query = analysis.get("query") or prompt
    
    # 3. Ищем треки
    tracks = await downloader.search(query=query, limit=15)
    if tracks:
        for track in tracks[:3]:
            asyncio.create_task(downloader.download(track.identifier, track))
            
    # Возвращаем и плейлист, и текст для озвучки!
    return {"playlist": tracks, "message": ai_message}

# Глушилка для favicon, чтобы не засорять логи 404 ошибкой
@app.get("/favicon.ico")
async def favicon():
    return JSONResponse(status_code=200, content={"status": "ok"})

@app.get("/api/player/playlist")
async def get_playlist(query: str, request: Request):
    downloader = request.app.state.downloader
    tracks = await downloader.search(query=query, limit=15)
    if tracks:
        for track in tracks[:3]:
            asyncio.create_task(downloader.download(track.identifier, track))
    return {"playlist": tracks}
    
@app.get("/stream/{video_id}")
async def stream_audio(video_id: str, request: Request):
    downloader = request.app.state.downloader
    download_result = await downloader.download(video_id)
    
    if download_result and download_result.success and download_result.file_path:
        logger.info(f"Serving local file: {download_result.file_path}")
        return FileResponse(download_result.file_path, media_type="audio/mpeg")

    return JSONResponse(status_code=404, content={"error": "Track not available"})

# Mount static files AFTER API routes
app.mount("/", StaticFiles(directory="static", html=True), name="static")
