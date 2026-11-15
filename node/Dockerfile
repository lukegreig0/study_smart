FROM python:3.13-slim

# Show log output in `docker logs` as it happens
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer is cached when only code changes
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY node/ ./node/

# Don't run as root
RUN useradd --system --no-create-home --uid 10001 app
USER app

# All configuration comes from environment variables (see README.md)
CMD ["python", "-m", "node"]
