FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/julian

# Install the pinned, reproducible set (not the loose requirements.txt)
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

# Non-root runtime user
RUN useradd --create-home julian && chown -R julian /srv/julian
USER julian

EXPOSE 8000

# Migrations run at boot, then the app starts.
# IMPORTANT: keep a single worker — the in-process scheduler and in-memory
# rate limiter assume one process. Scale vertically first; see
# docs/DEPLOYMENT.md before adding replicas.
CMD ["./start.sh"]
