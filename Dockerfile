FROM condaforge/miniforge3:latest

WORKDIR /app

RUN conda install -y \
  pandas=2.0.3 \
  numpy=1.24.3 \
  pytz \
  && pip install alpaca-py==0.33.1 python-dotenv==1.0.1 yfinance==0.2.43 \
  && conda clean -afy

COPY bot.py report.py notify.py status.py secrets.py db.py ./

CMD ["python", "bot.py"]
