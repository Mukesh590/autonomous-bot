# WHY THIS BASE IMAGE:
#   python:3.12-slim — Alpine has known issues with pandas/numpy C extensions.
#   slim gives us a minimal Debian base without the Alpine incompatibilities.
#   3.12 is the current stable LTS.
FROM python:3.12-slim

# Prevent .pyc files and enable unbuffered stdout (so Railway shows logs immediately)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (layer-cached; only re-runs when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY bot.py strategy.py risk_manager.py trade_logger.py ./

# logs directory — overridden by Railway volume mount if configured
RUN mkdir -p /app/logs

# The bot runs as a long-lived worker process (not a web server)
CMD ["python", "bot.py"]
