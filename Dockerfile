# syntax=docker/dockerfile:1.7

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

ARG VERSION=0.1.0
ARG VCS_REF=unknown
ARG PIP_INDEX_URL=https://pypi.org/simple
LABEL org.opencontainers.image.title="gh-aw-router HTTP sidecar" \
    org.opencontainers.image.description="Stateless classification planning and model routing" \
    org.opencontainers.image.source="https://github.com/githubnext/gh-aw-router" \
    org.opencontainers.image.revision="${VCS_REF}" \
    org.opencontainers.image.version="${VERSION}" \
    org.opencontainers.image.licenses="MIT"

ENV HOME=/home/gh-aw-router \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    GH_AW_ROUTER_ROUTING_TABLES=/routing

RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 gh-aw-router \
    && useradd --system --uid 10001 --gid gh-aw-router \
        --create-home --home-dir /home/gh-aw-router \
        --shell /usr/sbin/nologin gh-aw-router \
    && mkdir --parents /app/src /routing

COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install \
    --disable-pip-version-check \
    --index-url "${PIP_INDEX_URL}" \
    --no-cache-dir \
    --require-hashes \
    --requirement /tmp/requirements.lock \
    && rm /tmp/requirements.lock

COPY src /app/src
COPY routing /routing
COPY LICENSE /usr/share/doc/gh-aw-router/LICENSE

RUN find /app /routing -type d -exec chmod 0555 {} + \
    && find /app /routing -type f -exec chmod 0444 {} + \
    && chmod 0444 /usr/share/doc/gh-aw-router/LICENSE

USER 10001:10001
WORKDIR /home/gh-aw-router

EXPOSE 8737
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=5s --timeout=2s --start-period=2s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8737/healthz', timeout=1).close()"]

ENTRYPOINT ["python", "-m", "gh_aw_router"]
CMD ["serve", "--bind", "0.0.0.0:8737", "--log-level", "info"]
