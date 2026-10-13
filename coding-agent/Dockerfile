# syntax=docker/dockerfile:1

FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COW_ROOT=/cow \
    DUMMY_REPO=/opt/dummy-repo \
    HOST_ID=daemon \
    PORT=7700 \
    HOME=/home/daemon

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends git

RUN groupadd --system --gid 1000 daemon \
    && useradd --system --uid 1000 --gid daemon --home-dir /home/daemon --create-home daemon

WORKDIR /app
COPY --link daemon /app/daemon
COPY --link dummy-repo /opt/dummy-repo
RUN chown -R daemon:daemon /app /opt/dummy-repo

USER daemon

EXPOSE 7700

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7700/health', timeout=2).read()"]

ENTRYPOINT ["python", "-m", "daemon.server"]
