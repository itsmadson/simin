FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN adduser --disabled-password --gecos "" simin
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[research]"
COPY db ./db
USER simin
CMD ["python", "-m", "simin.cli", "--help"]
