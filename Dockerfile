FROM python:3.13-slim

# Iranian networks frequently cannot reach pypi.org directly. Override at build
# time with:  docker compose build --build-arg PIP_INDEX_URL=<reachable mirror>
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org pypi.doubanio.com pypi.mirrors.ustc.edu.cn" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && adduser --disabled-password --gecos "" simin

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e .

COPY db ./db
COPY docker ./docker
RUN chmod +x docker/*.sh && chown -R simin:simin /app

USER simin
CMD ["python", "-m", "simin.cli", "--help"]
