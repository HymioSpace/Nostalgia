ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NOSTALGIA_DATABASE=/data/nostalgia.sqlite3
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml README.md ./
COPY nostalgia ./nostalgia
RUN pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home nostalgia \
    && mkdir /data && chown nostalgia:nostalgia /data
USER nostalgia
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('NOSTALGIA_PORT','8000')+'/healthz', timeout=2)"
CMD ["python", "-m", "nostalgia"]
