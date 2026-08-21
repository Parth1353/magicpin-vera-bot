FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vera_bot ./vera_bot
COPY run.py .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT','8080') + '/v1/healthz', timeout=2)"

# Stateful for the length of a test window: the judge must reach the same process that
# received the context pushes, so this stays a single worker and a single instance.
CMD ["sh", "-c", "uvicorn vera_bot.app:app --host 0.0.0.0 --port ${PORT} --workers 1"]
