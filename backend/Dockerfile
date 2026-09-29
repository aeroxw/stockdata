FROM python:3.12-slim

WORKDIR /app

# 系统依赖：psycopg 编译需要
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

ENV PYTHONUNBUFFERED=1
EXPOSE 9850

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9850", "--workers", "2"]
