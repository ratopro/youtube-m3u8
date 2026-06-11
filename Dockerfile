FROM python:3.12-slim

ARG APP_VERSION=dev
ARG APP_COMMIT=unknown
ARG APP_BUILD_DATE=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Madrid \
    APP_VERSION=${APP_VERSION} \
    APP_COMMIT=${APP_COMMIT} \
    APP_BUILD_DATE=${APP_BUILD_DATE}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        g++ \
        gosu \
        nodejs \
        pkg-config \
        tzdata \
        libglib2.0-dev \
        libgstreamer1.0-dev \
        libgstreamer-plugins-base1.0-dev \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY sofa.mp4 .
COPY src ./src
COPY templates ./templates

RUN g++ -std=c++17 -O2 $(pkg-config --cflags gstreamer-1.0 glib-2.0) src/gstreamer_engine.cpp \
    -o /usr/local/bin/gstreamer_engine $(pkg-config --libs gstreamer-1.0 glib-2.0)

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

COPY --chown=root:root entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/health >/dev/null || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "5000"]
