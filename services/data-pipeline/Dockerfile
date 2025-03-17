FROM python:3.9-slim

# Install system dependencies
RUN apt-get update && apt-get install -y curl && pip install --default-timeout=100 poetry

# Set working directory
WORKDIR /app

# Create Python package structure
RUN mkdir -p /app/shared_utils/connections /app/shared_utils/haversine /app/shared_utils/credentials

# Copy shared utils first
COPY services/shared-utils/connections/*.py /app/shared_utils/connections/
COPY services/shared-utils/haversine/*.py /app/shared_utils/haversine/
COPY services/shared-utils/credentials /app/shared_utils/credentials/

# Create __init__.py files
RUN touch /app/shared_utils/__init__.py \
    && touch /app/shared_utils/connections/__init__.py \
    && touch /app/shared_utils/haversine/__init__.py

# Copy poetry files
COPY services/data-pipeline/pyproject.toml services/data-pipeline/poetry.lock ./

# Install dependencies
RUN poetry config virtualenvs.create false \
    && poetry install --no-root \
    && pip install python-decouple==3.8

# Copy application files
COPY services/data-pipeline/extract_firebase_data.py .
COPY services/data-pipeline/train_xgboost.py .

# Set PYTHONPATH explicitly
ENV PYTHONPATH=/app

CMD ["sh", "-c", "python extract_firebase_data.py && python train_xgboost.py"]