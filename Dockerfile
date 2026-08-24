FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code first (embeddings module needs the package), then bake the
# embedding model into the image so cold starts skip the ~35MB download.
COPY app ./app
RUN python -c "from app.services.embeddings import embed_texts; embed_texts(['warmup'])"

COPY scripts ./scripts
COPY data ./data

EXPOSE 10000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
