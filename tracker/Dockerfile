# cfoperator-tracker: stdlib HTTP service that hands remediation rows to an
# issue tracker (Plane / GitHub Issues / Jira).
#
# One image; CFOP_TRACKER_BACKEND picks the adapter at startup. Speaks the
# stable contract (POST /items, POST /items/{ref}/comment,
# POST /items/{ref}/transition, GET /items/{ref}) behind the cfop-tracker
# Service, reached by the agent via CFOP_TRACKER_URL. Stdlib-only at runtime.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 tracker
USER tracker
WORKDIR /home/tracker

COPY --chown=tracker:tracker shapes.py /home/tracker/shapes.py
COPY --chown=tracker:tracker http_client.py /home/tracker/http_client.py
COPY --chown=tracker:tracker backends.py /home/tracker/backends.py
COPY --chown=tracker:tracker backend_plane.py /home/tracker/backend_plane.py
COPY --chown=tracker:tracker backend_github.py /home/tracker/backend_github.py
COPY --chown=tracker:tracker backend_jira.py /home/tracker/backend_jira.py
COPY --chown=tracker:tracker entrypoint.py /home/tracker/entrypoint.py

ENV CFOP_TRACKER_HOST=0.0.0.0
ENV CFOP_TRACKER_PORT=8092
EXPOSE 8092

ENTRYPOINT ["python3", "/home/tracker/entrypoint.py"]
