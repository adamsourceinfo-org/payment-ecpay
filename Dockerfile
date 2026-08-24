# slim 而非 alpine：pydantic-core 在 musl 上取得 wheel 較不可靠，
# 為了省幾 MB 去冒建置失敗的風險不划算。
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY migrations/ ./migrations/
# Cloud Run 注入 PORT，預設 8080
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
