import asyncio
import logging
import shutil
import os
import time # 🟢 Добавить наверх, если нет
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, BotCommand
from telegram.ext import Application

from config import get_settings
from logging_setup import setup_logging
from ai_manager import AIManager
from youtube import YouTubeDownloader
from quiz_service import QuizManager
from radio import RadioManager
from chat_service import ChatManager
from cache_service import CacheService
from handlers import setup_handlers

logger = logging.getLogger("main")

# ⚠️ ВСЕ ТЯЖЕЛЫЕ ПРОЦЕССЫ ВЫНЕСЕНЫ В ФОН
async def lazy_startup_tasks(app: FastAPI):
    logger.info("⏳ Ленивая инициализация сервисов в фоне...")
    settings = app.state.settings
    tg_app = app.state.tg_app
    
    commands = [
        BotCommand("radio", "🎲 Случайная волна"), 
        BotCommand("play", "🔎 Найти трек"), 
        BotCommand("skip", "⏭ Следующий трек"), 
        BotCommand("stop", "🛑 Остановить"), 
        BotCommand("admin", "⚙️ Настройки"),
        BotCommand("quiz", "🎮 Игра 'Угадай мелодию'")
    ]
    
    connected = False
    attempt = 1
    while not connected:
        try:
            await tg_app.bot.set_my_commands(commands)
            await tg_app.initialize()
            await tg_app.start()
            
            if settings.WEBHOOK_URL:
                await tg_app.bot.set_webhook(url=settings.WEBHOOK_URL)
                logger.info(f"🔗 Webhook set to: {settings.WEBHOOK_URL}")
            else:
                # 🟢 ЕСЛИ НЕТ ВЕБХУКА - ЗАПУСКАЕМ ОПРОС (POLLING)
                if tg_app.updater:
                    await tg_app.updater.start_polling()
                    logger.info("📡 Webhook URL not found. Started Long Polling!")
            
            connected = True
            logger.info("🚀 Бот успешно подключен к Telegram API!")
        except Exception as e:
            logger.warning(f"⚠️ Сеть недоступна (Попытка {attempt}). Ждем 5 сек... Ошибка: {e}")
            attempt += 1
            await asyncio.sleep(5)

    # 🟢 Обновленный сборщик мусора
    downloads_dir = settings.DOWNLOADS_DIR
    while True:
        try:
            now = time.time()
            # Проверяем все временные расширения
            for ext in ("*.mp3", "*.mp4", "*.ogg"): 
                for file_path in downloads_dir.glob(ext):
                    # Если файлу больше часа (3600 сек) - удаляем
                    if file_path.is_file() and now - file_path.stat().st_mtime > 3600:
                        try:
                            file_path.unlink()
                            logger.debug(f"🗑 Сборщик мусора удалил: {file_path.name}")
                        except Exception as e: pass
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    app.state.settings = settings
    
    logger.info("⚡ System Starting Up (Railway Docker Edition)...")
    if shutil.which("ffmpeg"): logger.info("✅ FFmpeg detected.")
    else: logger.warning("⚠️ FFmpeg not found!")

    cache = CacheService(settings.CACHE_DB_PATH)
    await cache.initialize()
    
    ai_manager = AIManager(settings)
    chat_manager = ChatManager(ai_manager, cache) # 🟢 Передали cache
    downloader = YouTubeDownloader(settings, cache)
    
    builder = Application.builder().token(settings.BOT_TOKEN).read_timeout(30).write_timeout(30)
    tg_app = builder.build()
    
    # Сначала викторина, потом радио, чтобы передать зависимость
    quiz_manager = QuizManager(settings, downloader, chat_manager, cache) # 🟢 Передали cache
    radio_manager = RadioManager(
        bot=tg_app.bot, 
        settings=settings, 
        downloader=downloader, 
        chat_manager=chat_manager,
        quiz_manager=quiz_manager # Внедряем зависимость
    )
    
    # ⚠️ ИСПРАВЛЕНИЕ: Храним менеджеры в bot_data (официальный путь), а не в самом боте!
    tg_app.bot_data['radio_manager'] = radio_manager
    tg_app.bot_data['quiz_manager'] = quiz_manager
    
    # Привязываем к application, чтобы хендлеры могли их достать
    tg_app.ai_manager = ai_manager
    tg_app.chat_manager = chat_manager
    tg_app.downloader = downloader
    tg_app.settings = settings
    tg_app.cache = cache
    
    setup_handlers(tg_app)
    
    app.state.tg_app = tg_app
    app.state.chat_manager = chat_manager
    app.state.downloader = downloader
    
    startup_task = asyncio.create_task(lazy_startup_tasks(app))
    
    yield
    
    logger.info("🔻 System Shutting Down...")
    startup_task.cancel()
    await radio_manager.stop_all()
    # 🟢 Останавливаем updater (polling), если он работал
    if tg_app.updater and tg_app.updater.running:
        await tg_app.updater.stop() 
    await tg_app.stop()
    await tg_app.shutdown()
    await cache.close()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "engine": "Aurora Docker v3.5"}

@app.post("/telegram")
async def telegram_webhook(request: Request):
    try:
        tg_app = request.app.state.tg_app
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
        return JSONResponse(status_code=200, content={"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/player/playlist")
async def get_playlist(query: str, request: Request):
    try:
        downloader = request.app.state.downloader
        tracks = await downloader.search(query, limit=20)
        return {"playlist": [t.__dict__ for t in tracks]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/stream/{video_id}")
async def stream_audio(video_id: str, request: Request):
    downloader = request.app.state.downloader
    final_path = request.app.state.settings.DOWNLOADS_DIR / f"{video_id}.mp3"
    
    if final_path.exists():
        return FileResponse(path=final_path, media_type="audio/mpeg", headers={"Accept-Ranges": "bytes"})
        
    res = await downloader.download(video_id)
    if res.success and res.file_path:
        return FileResponse(path=res.file_path, media_type="audio/mpeg", headers={"Accept-Ranges": "bytes"})
    
    return JSONResponse(status_code=404, content={"error": "Not found"})

@app.get("/api/ai/dj")
async def api_ai_dj(prompt: str, request: Request):
    chat_manager = request.app.state.chat_manager
    ai_manager = request.app.state.tg_app.ai_manager
    downloader = request.app.state.downloader
    
    ai_message = await chat_manager.get_response(0, prompt, "Слушатель")
    
    analysis = await ai_manager.analyze_message(prompt)
    query = analysis.get("query") or prompt
    
    tracks = await downloader.search(query=query, limit=15)
    if tracks:
        for track in tracks[:3]:
            asyncio.create_task(downloader.download(track.identifier, track))
            
    return {"playlist": tracks, "message": ai_message}

@app.get("/favicon.ico")
async def favicon():
    return JSONResponse(status_code=200, content={"status": "ok"})

static_dir = Path("static")
if not static_dir.exists():
    static_dir.mkdir()
    
app.mount("/", StaticFiles(directory="static", html=True), name="static")

# Никаких if __name__ == "__main__": здесь больше нет!
