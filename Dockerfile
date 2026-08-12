FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# 1. Устанавливаем FFmpeg и Node.js 20.x
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# 2. Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# 3. ПРИНУДИТЕЛЬНО переустанавливаем yt-dlp-ejs, чтобы он зарегистрировал свои скрипты для yt-dlp
RUN pip install --no-cache-dir --upgrade --force-reinstall yt-dlp-ejs

COPY . .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
