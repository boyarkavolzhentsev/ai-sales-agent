# AI sales agent: one-shot runtime commands (python -m app.runtime <command>).
# One image for every deployment: configuration, secrets, credentials, the database and the
# approved knowledge are supplied at run time (environment + the /data volume), never baked in.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Unprivileged runtime user; it owns only the writable data volume.
RUN groupadd --system --gid 10001 agent \
    && useradd --system --uid 10001 --gid agent --home-dir /app --no-create-home --shell /usr/sbin/nologin agent

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Runtime code only (see .dockerignore: no tests, docs, .env, .local, databases or caches).
COPY app/ ./app/

# /data holds everything that must survive restarts: the SQLite database, the Gmail token
# (and OAuth client file, if used) and the deployment's approved knowledge directory.
RUN mkdir -p /data/db /data/credentials /data/knowledge \
    && chown -R agent:agent /data \
    && chmod 0750 /data /data/db /data/credentials
VOLUME ["/data"]

USER agent

ENTRYPOINT ["python", "-m", "app.runtime"]
# Read-only readiness report by default; pass any other command explicitly.
CMD ["deployment-check"]
