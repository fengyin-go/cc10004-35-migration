FROM python:3.12-slim
WORKDIR /srv/app
COPY requirements.txt ./
# 测试依赖留在开发机；镜像只装 requirements.txt 里的运行时包。
RUN sed -e '/^#/d' -e '/^$/d' -e '/pytest/d' -e '/httpx/d' requirements.txt > requirements-runtime.txt
RUN pip install --no-cache-dir -r requirements-runtime.txt \
    && rm -f requirements-runtime.txt
COPY app ./app
COPY run.sh ./run.sh
# 制品与 SQLite 都落在持久卷；容器内固定路径，由 compose/docker run 挂载覆盖。
ENV APP_DATA_DIR=/data \
    SQLITE_PATH=/data/app.sqlite3 \
    ARTIFACT_STORAGE_DIR=/data/artifacts
RUN mkdir -p /data/artifacts
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
