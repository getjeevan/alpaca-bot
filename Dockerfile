FROM continuumio/miniconda3:latest

WORKDIR /app

# Install core dependencies with conda (handles glibc compatibility)
RUN conda install -y -c conda-forge \
    python=3.11 \
    numpy=1.24.3 \
    pandas=2.0.3 \
    pytz \
    && conda clean -afy

# Install pip-only packages
COPY requirements.txt .
RUN pip install --no-cache-dir alpaca-py==0.33.1 python-dotenv==1.0.1 yfinance==0.2.43

# Copy application files
COPY bot.py report.py notify.py status.py secrets.py db.py ./

# Create logs directory
RUN mkdir -p /app/logs

# Run bot
CMD ["python", "bot.py"]
