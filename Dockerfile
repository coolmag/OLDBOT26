FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/x/install/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
# Cache buster to force re-running pip install
RUN echo "Cache buster: $(date +%s)"
# Принудительное обновление yt-dlp для борьбы с частыми блокировками
RUN pip install --no-cache-dir -r requirements.txt && pip install -U yt-dlp

COPY . .

CMD ["python", "main.py"]
