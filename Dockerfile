FROM python:3.11-slim

# Set environment variables to optimize Python runtime in Docker
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy and install requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code and configuration files
COPY config ./config
COPY app ./app

# Expose port 8000 for documentation (Render will map to the dynamic $PORT env var)
EXPOSE 8000

# Start Uvicorn, dynamically binding to the port specified by Render's PORT environment variable
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
