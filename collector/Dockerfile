FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 注意：本文件需以「项目根目录」为 build context 构建
#   docker build -f collector/Dockerfile .
# 因为采集器复用了 backend 的适配层，要把 backend/app 一起拷进来
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir apscheduler==3.11.0

COPY collector/collector.py ./collector.py
COPY backend/app/ ./backend/app/

ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "collector.py"]
