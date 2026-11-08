# 运行时只需要 FastAPI + uvicorn；核心流水线本身就是零依赖的
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.27"

COPY aqa ./aqa
COPY tools ./tools
COPY examples ./examples

# 产物、缺陷库、源码 checkout 都用 volume 挂进来（见 docker-compose.yml）
ENV AQA_SOURCE_DIR=/app/checkout \
    AQA_BUILD_KIND=package \
    AQA_DEVICE_KIND=sim \
    AQA_REQUIRE_HUMAN_CONFIRM=false

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

CMD ["uvicorn", "aqa.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
