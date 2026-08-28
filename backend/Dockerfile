# Simin backend.
#
# Two stages so the runtime image carries no build toolchain. The wheel is built
# once and installed into a clean layer, which keeps the final image small and
# means a compromised container has no compiler in it.

FROM python:3.12-slim AS build

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN pip install --no-cache-dir hatchling

COPY pyproject.toml README.md* ./
COPY src ./src
RUN pip wheel --no-deps --wheel-dir /wheels . \
 && pip wheel --wheel-dir /wheels .


FROM python:3.12-slim AS runtime

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SIMIN_DATA_DIR=/data

# curl is here only for the healthcheck below. Nothing else needs it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels simin \
 && rm -rf /wheels

# Run unprivileged. A trading bot with API keys in its environment is exactly
# the process that should not be root.
RUN useradd --create-home --uid 10001 simin \
 && mkdir -p /data && chown -R simin:simin /data
USER simin

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "simin.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
