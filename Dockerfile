FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copy & install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade --no-cache-dir pip setuptools wheel && \
    pip install --no-cache-dir pytz -r requirements.txt

# Copy application files (including new modules)
COPY bot.py report.py notify.py status.py secrets.py db.py ./

# Create logs directory
RUN mkdir -p /app/logs

# Run bot (will load secrets from environment, not .env)
CMD ["python3", "bot.py"]
