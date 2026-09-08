FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# База данных должна жить на постоянном диске (volume), иначе сотрётся при передеплое.
# Railway: примонтируй volume в /data и задай переменную DB_PATH=/data/bot.db
VOLUME ["/data"]

CMD ["python", "bot.py"]
