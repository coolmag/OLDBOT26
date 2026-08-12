FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# 1. Устанавливаем FFmpeg и QuickJS (легкий JS-движок, не падает от OOM в Railway)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    ca-certificates \
    quickjs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# 2. Устанавливаем зависимости (yt-dlp и yt-dlp-ejs)
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
