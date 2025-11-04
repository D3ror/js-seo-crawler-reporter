# Simpler Dockerfile using Playwright's official Python image
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# Make sure Python output is unbuffered
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# 1) Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# 2) Copy the rest of the app
COPY . /app

# 3) Expose port and run FastAPI via uvicorn
EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
