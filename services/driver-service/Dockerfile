FROM python:3.9-slim
RUN apt-get update && apt-get install -y curl && pip install --default-timeout=100 poetry

# Set working directory
WORKDIR /app

# Copy Poetry files and install dependencies
COPY services/driver-service/pyproject.toml services/driver-service/poetry.lock ./
COPY services/shared-utils/ ./shared-utils/
RUN poetry config virtualenvs.create false && poetry install --without dev --no-root

# Copy application code (includes metrics.py if in src/helper_func/)
COPY services/driver-service/src/ .

# Copy Firebase credentials
COPY services/shared-utils/credentials/riderx-4f28f-firebase-adminsdk-fbsvc-dfb7d4d54e.json /app/shared-utils/credentials/

# Expose ports for the service and metrics
EXPOSE 5001 8001

# Run the application
CMD ["python", "run.py"]