FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config.yaml /app/config.yaml

VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "cerberus", "--config", "/app/config.yaml"]
