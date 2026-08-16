# Multi-stage build for smaller final image
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on

WORKDIR /app

# Install system dependencies for building Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --user --no-warn-script-location -r requirements.txt

# Asset stage: compile Tailwind. Kept separate from the Python builder so a
# template-only change does not reinstall pip packages, and vice versa.
FROM node:20-alpine AS assets

WORKDIR /build

COPY package.json package-lock.json ./
RUN npm ci

# Tailwind scans templates and forms.py to decide which classes to emit, so
# both have to be present before the build runs.
COPY tailwind.config.js postcss.config.js ./
COPY ttstats/ ./ttstats/
RUN npm run build:css

# Final stage
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /root/.local /root/.local

# Make sure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH

# Copy application code from ttstats subdirectory
COPY ttstats/ .

# Built CSS must land AFTER the source copy above, or it gets overwritten by
# the (empty) css/ directory that ships in the repo.
COPY --from=assets /build/ttstats/pingpong/static/pingpong/css/app.css \
     /app/pingpong/static/pingpong/css/app.css

# Copy and make entrypoint executable
COPY docker/django/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Collect static files directory
RUN mkdir -p /app/static

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
