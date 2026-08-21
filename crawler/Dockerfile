FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY lodestone_crawler ./lodestone_crawler

RUN pip install --no-cache-dir .

# Non-root: the crawler needs no privileges beyond outbound HTTPS.
RUN useradd --create-home --uid 10001 lodestone
USER lodestone

ENTRYPOINT ["python", "-m", "lodestone_crawler"]
CMD ["probe"]
