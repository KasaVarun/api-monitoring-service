FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATABASE_URL=sqlite+aiosqlite:////data/monitor.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pyproject.toml .

RUN mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=3)"

CMD ["python", "-m", "app.launch"]
