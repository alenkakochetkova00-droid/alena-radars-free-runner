FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOT_DATA_DIR=/data

WORKDIR /app

COPY travel-radar/requirements.txt /tmp/travel-requirements.txt
RUN pip install --no-cache-dir -r /tmp/travel-requirements.txt

COPY . /app
RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "cloud_runner.py"]
