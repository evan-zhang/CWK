FROM python:3.11-slim
WORKDIR /app
COPY adapters /app/adapters
ENV PYTHONUNBUFFERED=1
EXPOSE 8790
CMD ["python", "-m", "adapters.rag_answer.server"]
