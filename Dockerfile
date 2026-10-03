FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

EXPOSE 8000

# 启动时自动完成建库建表（见 app.db.init_db）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
