FROM python:3.11-slim

WORKDIR /app
COPY adapters /app/adapters

ENV PYTHONUNBUFFERED=1
EXPOSE 8787
CMD ["python3", "-m", "adapters.opensearch_retrieval", "serve", "--host", "0.0.0.0"]
