FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -e . && playwright install chromium && playwright install-deps

ENTRYPOINT ["argus-mcp"]
