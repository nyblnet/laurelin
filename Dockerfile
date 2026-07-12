# Laurelin server image. The web UI is prebuilt and committed
# (laurelin/ui/static/index.html), so no Node toolchain is needed here — just
# install the Python package and run.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first (better layer caching), then the package.
COPY pyproject.toml README.md LICENSE ./
COPY laurelin ./laurelin
RUN pip install ".[postgres]"

# Non-root runtime user; workspaces/data live under /data (mount a volume).
RUN useradd --create-home --uid 10001 laurelin \
    && mkdir -p /data && chown -R laurelin:laurelin /data /app
USER laurelin

EXPOSE 8787
VOLUME ["/data"]

# Default: multi-workspace server over /data. Override the command for
# single-workspace mode (e.g. `laurelin serve --workspace /data/ws`).
# In production also set LAURELIN_CONTROL_DATABASE_URL to a Postgres URL and
# LAURELIN_SECURE_COOKIES/secure-cookies behind TLS.
CMD ["laurelin", "serve", "--root", "/data", "--host", "0.0.0.0", "--port", "8787", "--secure-cookies"]
