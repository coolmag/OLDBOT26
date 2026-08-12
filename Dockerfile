FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# 1. Устанавливаем FFmpeg, Node.js, UNZIP (нужен для Deno) и сам Deno
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    # Устанавливаем Deno 2.x (предпочтительный рантайм для yt-dlp в Docker)
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# 2. Устанавливаем Python-зависимости (yt-dlp и yt-dlp-ejs)
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
