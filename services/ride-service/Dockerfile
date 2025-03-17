FROM python:3.9-slim
RUN pip install poetry
WORKDIR /app
COPY pyproject.toml poetry.lock ./
COPY shared-utils/ ./shared-utils/
RUN poetry config virtualenvs.create false && poetry install --no-dev
COPY . .
COPY xgboost_model.pkl .
EXPOSE 5002 8002
CMD ["python", "ride.py"]