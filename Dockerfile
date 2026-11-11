# syntax=docker/dockerfile:1
# Built by GitHub Actions (see .github/workflows/release.yml), not on the deployment host.

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY requirements.lock requirements-build.lock ./
RUN python -m venv /venv && python -m venv /buildenv \
 && /venv/bin/pip install --require-hashes --no-deps -r requirements.lock \
 && /buildenv/bin/pip install --require-hashes --no-deps -r requirements-build.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN /buildenv/bin/pip wheel --no-deps --no-build-isolation -w /wheels . \
 && /venv/bin/pip install --no-deps /wheels/*.whl

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/bask209/smarrtbot" \
      org.opencontainers.image.description="Telegram bot: chat to Radarr/Sonarr via Gemini, hardlink-aware storage cleanup" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
ENV PATH=/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SMARRTBOT_CONFIG=/config/config.yaml \
    SMARRTBOT_DATA_DIR=/data/state
# Pick up Debian security fixes released after the pinned base digest (Trivy gates the release).
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get -y upgrade \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data/state /config && chown 1000:1000 /data/state
COPY --from=build /venv /venv
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 CMD ["smarrtbot", "healthcheck"]
ENTRYPOINT ["smarrtbot"]
CMD ["run"]
