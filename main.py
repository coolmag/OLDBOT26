# Version: 50 - DI Refactor
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
from proxy_service import ProxyManager
from ai_manager import AIManager
from chat_service import ChatManager

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Инициализация базовых сервисов
    setup_logging()
    settings = get_settings()
    app.state.settings = settings
    
    logger.info("⚡ System Starting Up...")
    if shutil.which("ffmpeg"): logger.info("✅ FFmpeg detected.")
    else: logger.critical("❌ FFmpeg not found! Audio processing will fail.")

    # 2. Инициализация кэша
    cache = CacheService(settings.CACHE_DB_PATH)
    await cache.initialize()
    
    # 3. Инициализация и запуск демона прокси
    proxy_manager = ProxyManager(settings.V2RAY_PROXIES_FILE)
    await proxy_manager.start_daemon()
    
    # 4. Инициализация AI и Chat менеджеров (с DI)
    ai_manager = AIManager(settings)
    chat_manager = ChatManager(ai_manager)
    
    # 5. Инициализация загрузчиков и сервисов
    downloader = YouTubeDownloader(settings, cache, proxy_manager)
    spotify_service = SpotifyService(settings, downloader)
    
    # 6. Сборка и настройка Telegram приложения
    builder = Application.builder().token(settings.BOT_TOKEN).read_timeout(30).write_timeout(30)
    tg_app = builder.build()
    
    radio_manager = RadioManager(bot=tg_app.bot, settings=settings, downloader=downloader)
    
    # 7. Внедрение всех зависимостей в контекст Telegram
    # Теперь хендлеры могут получать их через context.application.*
    tg_app.ai_manager = ai_manager
    tg_app.chat_manager = chat_manager
    tg_app.downloader = downloader
    tg_app.radio_manager = radio_manager
    tg_app.spotify_service = spotify_service
    tg_app.settings = settings
    tg_app.cache = cache
    
    # 8. Регистрация хендлеров (упрощенная сигнатура)
    setup_handlers(tg_app)
    
    commands = [
        BotCommand("radio", "🎲 Случайная волна"),
        BotCommand("play", "🔎 Найти трек"),
        BotCommand("stop", "🛑 Остановить"),
        BotCommand("admin", "⚙️ Настройки"),
    ]
    await tg_app.bot.set_my_commands(commands)
    
    # 9. Запуск Telegram бота
    await tg_app.initialize()
    await tg_app.start()
    
    if settings.WEBHOOK_URL:
        await tg_app.bot.set_webhook(url=settings.WEBHOOK_URL)
        logger.info(f"🔗 Webhook set to: {settings.WEBHOOK_URL}")
    
    # 10. Передача ключевых сервисов в состояние FastAPI для веб-эндпоинтов
    app.state.tg_app = tg_app
    app.state.chat_manager = chat_manager
    app.state.downloader = downloader
    
    yield
    
    # --- Shutdown Logic ---
    logger.info("🔻 System Shutting Down...")
    await radio_manager.stop_all()
    await proxy_manager.stop_daemon() # Gracefully stop proxy daemon
    await tg_app.stop()
    await tg_app.shutdown()
    await cache.close()
    logger.info("🛑 System Stopped.")

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- API Endpoints ---
@app.post("/telegram")
async def telegram_webhook(request: Request):
    tg_app = request.app.state.tg_app
    try:
        update = Update.de_json(await request.json(), tg_app.bot)
        await tg_app.process_update(update)
    except Exception as e:
        logger.error(f"Webhook processing error: {e}", exc_info=True)
    return {"ok": True}

@app.get("/api/player/playlist")
async def get_playlist(query: str, request: Request):
    downloader = request.app.state.downloader
    tracks = await downloader.search(query=query, limit=15)
    # Pre-cache first few tracks without awaiting
    if tracks:
        for track in tracks[:3]:
            asyncio.create_task(downloader.download(track.id, track))
    return {"playlist": tracks}
    
@app.get("/stream/{video_id}")
async def stream_audio(video_id: str, request: Request):
    downloader = request.app.state.downloader
    download_result = await downloader.download(video_id)
    if download_result.success:
        return FileResponse(download_result.file_path, media_type="audio/mpeg")
    return JSONResponse(status_code=404, content={"error": "Track not available"})

# Mount static files AFTER API routes
app.mount("/", StaticFiles(directory="static", html=True), name="static")
