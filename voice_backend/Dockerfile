FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ffmpeg && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry

COPY voice_backend/pyproject.toml /app/pyproject.toml
COPY voice_backend/poetry.lock /app/poetry.lock
RUN poetry config virtualenvs.create false && poetry install --no-interaction --no-ansi

COPY voice_backend /app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
