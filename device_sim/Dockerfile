FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sim.py .

# Show print() output in `docker logs` as it happens
ENV PYTHONUNBUFFERED=1

USER nobody

CMD ["python", "sim.py"]
