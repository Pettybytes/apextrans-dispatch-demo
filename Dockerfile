FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=8090 \
    DISPATCH_DATA_DIR=/app/data

WORKDIR /app

# Standard library only - nothing to install.
COPY requirements.txt .
COPY dispatch_core.py app.py openapi.json ./
COPY data/ ./data/
COPY static/ ./static/
COPY config/ ./config/
COPY approval_limits.md ./

EXPOSE 8090

HEALTHCHECK --interval=15s --timeout=3s --start-period=3s --retries=5 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=2).status==200 else 1)"

CMD ["python3", "app.py"]
