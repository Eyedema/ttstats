# Multi-stage build for smaller final image
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on

WORKDIR /app

# No compiler here on purpose. Every pinned dependency ships a manylinux wheel
# (psycopg2-binary, cryptography, cffi included), so build-essential and
# libpq-dev bought nothing but ~6 minutes of apt and a memory spike big enough
# to OOM the 3.7GB VPS this used to build on.
#
# --only-binary=:all: keeps it that way: adding a package that needs compiling
# fails here, loudly, instead of silently reintroducing the toolchain.
#
# http-ece is the one exception, and it is a safe one: pywebpush needs it for
# the payload encryption, and it publishes an sdist and no wheel at all. It is
# pure Python, so building it needs setuptools and nothing else -- no
# compiler, no apt, none of what the rule above exists to prevent. Any OTHER
# package that wants an exception here should be treated as a red flag.
COPY requirements.txt .
RUN pip install --user --no-warn-script-location \
    --only-binary=:all: --no-binary=http-ece \
    -r requirements.txt

# Asset stage: compile Tailwind. Kept separate from the Python builder so a
# template-only change does not reinstall pip packages, and vice versa.
FROM node:20-alpine AS assets

WORKDIR /build

COPY package.json package-lock.json ./
RUN npm ci

# Tailwind scans templates and forms.py to decide which classes to emit, so
# both have to be present before the build runs.
COPY tailwind.config.js postcss.config.js ./
# scripts/ carries vendor_assets.mjs, which `npm run build` invokes.
COPY scripts/ ./scripts/
COPY ttstats/ ./ttstats/
RUN npm run build

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

# Built CSS and vendored JS must land AFTER the source copy above, or they get
# overwritten by the (empty) css/ directory that ships in the repo.
COPY --from=assets /build/ttstats/pingpong/static/pingpong/css/ \
     /app/pingpong/static/pingpong/css/
COPY --from=assets /build/ttstats/pingpong/static/pingpong/js/vendor/ \
     /app/pingpong/static/pingpong/js/vendor/
# Archivo. app.css url()s these; prod's manifest storage resolves every url()
# at collectstatic time and entrypoint.sh runs it under `set -e`, so a missing
# font file fails the deploy rather than just the page.
COPY --from=assets /build/ttstats/pingpong/static/pingpong/fonts/ \
     /app/pingpong/static/pingpong/fonts/

# Copy and make entrypoint executable
COPY docker/django/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Collect static files directory
RUN mkdir -p /app/static

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
