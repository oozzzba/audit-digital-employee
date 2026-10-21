# 审计数字员工 · 生产镜像
# 单 worker + SQLite 持久化；DeepSeek API Key 由运行时环境变量注入，绝不打进镜像。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖（利用构建缓存），再拷代码
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# 单 worker：当前为 SQLite + 进程内缓存；要横向扩容需先共享存储 / sticky session 后再开多 worker
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
