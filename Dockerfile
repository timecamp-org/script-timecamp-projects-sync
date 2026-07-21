FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TIMECAMP_MANDATORY_TAG_CACHE_FILE=/tmp/timecamp_mandatory_tag_cache.json

WORKDIR /app

COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 . .

USER 10001:10001

CMD ["sh", "-c", "python3 fetch_datadog.py --output /tmp/tasks.json && python3 sync_projects.py --input /tmp/tasks.json"]
