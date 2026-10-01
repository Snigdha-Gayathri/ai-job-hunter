# Production Runtime Dockerfile for AI Job Hunter Persistent Worker Daemon
FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV RUN_MODE=worker
ENV STATE_DIR=/data

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY agent/ ./agent/

# Create persistent state directory mount point
RUN mkdir -p /data

# Volume for durable state persistence across container restarts
VOLUME ["/data"]

# Run the persistent polling worker daemon
CMD ["python", "agent/main.py", "--worker"]
