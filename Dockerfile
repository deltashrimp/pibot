# made by triple-raze (aka рамзес)
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY source/ ./source/
COPY bot-data/ ./bot-data/
COPY info/ ./info/

CMD ["python", "source/pibot.py"]
