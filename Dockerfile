FROM python:3.12-slim

WORKDIR /app

# system deps (none needed beyond stdlib for this bot)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Render provides $PORT; bot reads WEBHOOK_URL, BOT_TOKEN, OWNER_ID,
# D1_DATABASE_ID, D1_API_TOKEN from env.
CMD ["python", "bot.py"]
