# Stage-1 image: real Google Chrome + Xvfb + x11vnc + the friday app.
# Google ships Chrome for Linux as amd64 only, hence the pinned platform.
# (Hardening and full docs land in stage 7.)
FROM --platform=linux/amd64 python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xvfb x11vnc xauth tini \
      fonts-liberation fonts-noto-core fonts-noto-cjk fonts-noto-color-emoji tzdata util-linux \
 && curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -rf /tmp/chrome.deb /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.13.1
RUN useradd --create-home --uid 1000 friday
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY friday ./friday
COPY scripts ./scripts
RUN uv sync --frozen --no-dev && sh scripts/fetch_novnc.sh && chown -R friday /app \
 && mkdir -p /data && chown friday /data && chmod 700 /data

USER friday
ENV FRIDAY_DATA_DIR=/data FRIDAY_BIND_HOST=0.0.0.0 FRIDAY_DISPLAY_BACKEND=xvfb PATH="/app/.venv/bin:$PATH"
VOLUME /data
EXPOSE 8765
# tini reaps Chrome helper processes that would otherwise linger as zombies.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["friday", "serve"]
