FROM python:3.13-slim

# Show log output in `docker logs` as it happens
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/studyspace.db

WORKDIR /app

# Install dependencies first so this layer is cached when only code changes
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY host/ ./host/

# Don't run as root. /data must exist and belong to that user *before* the
# VOLUME line: a fresh named volume is initialised from the image's directory,
# ownership included, so the host can create the database in it.
RUN useradd --system --no-create-home --uid 10001 app \
 && mkdir /data && chown app:app /data
VOLUME /data
USER app

# All configuration comes from environment variables (see README.md)
CMD ["python", "-m", "host"]
