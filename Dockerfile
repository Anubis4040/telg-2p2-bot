FROM python:3.11-slim

WORKDIR /app

RUN mkdir -p /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py /app/bot.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
