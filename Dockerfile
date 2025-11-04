# Dockerfile (recommended - Python 3.11 + Playwright)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8080

WORKDIR /app

# 1) Install system deps required for Chromium and building wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg wget \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxrandr2 libasound2 libgbm1 libpangocairo-1.0-0 libxdamage1 \
    libxext6 libgdk-pixbuf2.0-0 libglib2.0-0 libgtk-3-0 libpango-1.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxfixes3 libxrender1 libxss1 libxtst6 \
    fonts-liberation libappindicator3-1 lsb-release xdg-utils \
    build-essential && \
    rm -rf /var/lib/apt/lists/*

# 2) Copy only requirements first (caching)
COPY requirements.txt /app/requirements.txt

# 3) Install Python deps
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# 4) Copy the rest of the app
COPY . /app

# 5) Install Playwright browsers
# Use python -m playwright to ensure CLI installed via pip is available
RUN python -m playwright install chromium

# 6) Expose port and run uvicorn
EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]