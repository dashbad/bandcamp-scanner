FROM python:3.14-slim

# Standard library only: no dependencies to install.
WORKDIR /app
COPY scanner.py ./
COPY static ./static

ENV PYTHONUNBUFFERED=1 \
    BCS_HOST=0.0.0.0 \
    BCS_PORT=8765 \
    BCS_DB=/data/releases.db

EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"

CMD ["python", "scanner.py"]
