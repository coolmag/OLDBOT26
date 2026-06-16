FROM python:3.11-slim

# Установка системных зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Установка Deno
RUN curl -fsSL https://deno.land/x/install/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .

# Установка PyTorch (CPU-версия)
RUN pip install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
# Установка остальных зависимостей
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
