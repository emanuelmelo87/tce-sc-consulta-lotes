FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
CMD exec gunicorn app:app \
    --worker-class gthread \
    --workers 1 \
    --threads 16 \
    --timeout 3600 \
    --bind 0.0.0.0:$PORT
