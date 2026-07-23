FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir . \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["argus-mcp"]
