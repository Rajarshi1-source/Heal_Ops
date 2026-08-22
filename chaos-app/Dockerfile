# One image, four faults. The SERVICE env var selects which one runs.
#
# A single image keeps the chaos build to one layer cache and one tag to pin, and it means the
# four services are guaranteed to share a base -- so a difference in their behaviour is the fault
# being injected, not a difference in the runtime underneath.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --gid 10002 chaos \
    && useradd --uid 10002 --gid chaos --create-home --shell /usr/sbin/nologin chaos \
    && mkdir -p /var/log/chaos \
    && chown chaos:chaos /var/log/chaos

WORKDIR /app
COPY --chown=chaos:chaos services/ ./services/
COPY --chown=chaos:chaos entrypoint.py ./entrypoint.py

USER chaos
EXPOSE 8080

# Deliberately no HEALTHCHECK: crashy-api's whole job is to fail, and a Docker-level healthcheck
# would compete with the kubelet's probes for the signal the demo depends on.
ENTRYPOINT ["python", "/app/entrypoint.py"]
