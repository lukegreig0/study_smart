FROM python:3.13-slim

# Show log output in `docker logs` as it happens
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/studyspace.db \
    API_HOST=0.0.0.0 \
    API_PORT=8000

WORKDIR /app

# Install dependencies first so this layer is cached when only code changes
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY api/ ./api/

# Same uid as the host container (10001), on purpose. The API opens the
# database read-only, but in WAL mode even a reader writes to the shared
# "-shm" index file, which the host creates as its own user. Matching uids
# means the API can use it.
RUN useradd --system --no-create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["python", "-m", "api"]
