FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY source/ ./source/
COPY bot-data/ ./bot-data/
COPY info/ ./info/
COPY .env ./.env

ENV TELEGRAM_TOKEN=""
ENV GROQ_KEY=""

CMD ["python", "source/pibot.py"]
