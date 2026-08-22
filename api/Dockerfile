FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY lodestone_api ./lodestone_api
RUN pip install --no-cache-dir .
COPY static ./static

RUN useradd --create-home --uid 10002 lodestone
USER lodestone

EXPOSE 8000
CMD ["uvicorn", "lodestone_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
