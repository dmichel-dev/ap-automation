FROM python:3.13.18-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# /data/files is created here so the named volume inherits the core user's ownership
RUN useradd --system --uid 10001 core \
 && mkdir -p /data/files && chown -R core /data/files && chmod 700 /data/files

# --- test image: adds pytest, PDF generation and Noto fonts (Latin, CJK; Arabic and
# Devanagari). Source and tests are mounted by the core-test service.
FROM base AS test
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-core fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
USER core
CMD ["pytest", "-q"]

# --- runtime image (default)
FROM base AS runtime
COPY app ./app
RUN chown -R core /app
USER core

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
