FROM python:3.9-slim
RUN pip install poetry
WORKDIR /app
COPY pyproject.toml poetry.lock ./
COPY ../shared-utils/ ../shared-utils/
RUN poetry config virtualenvs.create false && poetry install --no-dev
COPY . .
EXPOSE 5003 8003
CMD ["python", "payment.py"]