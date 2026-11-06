FROM python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

# Build-time network installs the pinned Python dependencies; the run is offline.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock

COPY .runtime/bin/nats-server /usr/local/bin/nats-server
WORKDIR /app
# Explicit source inventory: never inherit a developer's extra venv or runtime.
COPY .github /app/.github
COPY app /app/app
COPY scripts /app/scripts
COPY schemas /app/schemas
COPY config /app/config
COPY web /app/web
COPY packaging /app/packaging
COPY tests /app/tests
COPY docs /app/docs
COPY skills /app/skills
COPY infra /app/infra
COPY facts /app/facts
COPY release /app/release
COPY offline-bundle /app/offline-bundle
COPY pyproject.toml requirements.lock uv.lock README.md .gitignore .dockerignore /app/
COPY .runtime/bin/nats-server /app/.runtime/bin/nats-server
# Source files copied with a restrictive local umask are unreadable for a non-root
# container user; the cold-start drill runs as the invoking host UID (so the evidence
# bind mount is not left root-owned), so make the tree readable for everyone.
RUN chmod -R a+rX /app
ENV PYTHONUNBUFFERED=1 NATS_SERVER=/usr/local/bin/nats-server

CMD ["python", "-c", "print('company-agent offline image ready')"]
