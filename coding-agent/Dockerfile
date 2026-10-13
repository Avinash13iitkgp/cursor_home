FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COW_ROOT=/cow \
    DUMMY_REPO=/opt/dummy-repo \
    HOST_ID=daemon \
    PORT=7700 \
    HOME=/home/agent

RUN groupadd --system --gid 1000 agent \
    && useradd --system --uid 1000 --gid agent --home-dir /home/agent --create-home agent

WORKDIR /app
COPY daemon /app/daemon
COPY dummy-repo /opt/dummy-repo
RUN chown -R agent:agent /app /opt/dummy-repo

USER agent

EXPOSE 7700

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7700/health', timeout=2).read()"]

ENTRYPOINT ["python", "-m", "daemon.server"]
