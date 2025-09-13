FROM python:3.12-slim

WORKDIR /app

COPY metrics_collector.py .

RUN pip install flask prometheus_client requests

EXPOSE 8000

CMD ["python", "metrics_collector.py"]

