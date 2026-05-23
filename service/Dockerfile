# Single-stage build. Image stays small because Pango+Cairo dominate the size
# and there's nothing to compile away. ~250MB final image.
#
# Why slim and not Alpine: WeasyPrint needs glibc + fontconfig, and Alpine's
# musl + missing font setup makes PDF rendering unreliable.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Native deps for WeasyPrint + DejaVu fonts so the PDF renders identically to
# the macOS dev environment. fontconfig refreshes the font cache at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz-subset0 \
        libcairo2 \
        libffi8 \
        shared-mime-info \
        fonts-dejavu \
        fonts-dejavu-extra \
        fonts-liberation \
        fontconfig \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching — only re-runs when requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then copy the rest of the service.
COPY . .

# Railway injects $PORT at runtime. Default to 8000 for local docker-run.
ENV PORT=8000
EXPOSE 8000

# --proxy-headers so request.url reflects the Railway edge proxy.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=*"]
