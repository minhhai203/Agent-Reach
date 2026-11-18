# video-tracker — image nhỏ, chạy bằng user thường (uid 10001).
# Không cần ffmpeg: yt-dlp chỉ đọc metadata (--skip-download).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    TRACKER_DB=/data/tracker.sqlite3 \
    TRACKER_HOST=0.0.0.0 \
    TRACKER_PORT=8765

WORKDIR /app

# pyproject.toml khai báo readme = "README.md" nên phải COPY cả file này.
COPY pyproject.toml README.md ./
COPY video_tracker ./video_tracker

# Phiên bản phụ thuộc được chốt trong pyproject.toml (mcp 1.x, pydantic>=2.11, tzdata).
RUN pip install . \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin tracker \
    && mkdir -p /data \
    && chown 10001:10001 /data

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('TRACKER_PORT', '8765'), timeout=4)"]

ENTRYPOINT ["tracker"]
CMD ["serve", "--scheduler"]
