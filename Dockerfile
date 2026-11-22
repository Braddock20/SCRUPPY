FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .

RUN mkdir -p /app/state /app/logs /app/secrets \
    && chmod 700 /app/secrets

# --headless is explicit because the CLI flag overrides the YAML setting.
# config.yaml ships with behaviour.baseline_only: true for read-only testing.
CMD ["python", "-m", "igbot", "--headless", "watch"]
