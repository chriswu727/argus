FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY argus/ ./argus/

RUN pip install --no-cache-dir --upgrade 'pip>=26.2.1' 'setuptools>=83.0.0' \
    && pip install --no-cache-dir . \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["argus-mcp"]
