FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data /app/logs
VOLUME ["/app/data"]

# Optional: HTTP side-car (health / metrics / webhook). Set HTTP_PORT=8080 in .env to enable.
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; p=os.getenv('HTTP_PORT'); sys.exit(0) if not p else urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=4)" || exit 1

# SIGTERM → graceful shutdown (finish in-flight handlers, stop scheduler, close DB)
STOPSIGNAL SIGTERM
CMD ["python", "main.py"]
