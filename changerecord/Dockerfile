# cfoperator-changerecord: stdlib HTTP change-record service (github image).
#
# Speaks the stable 3-endpoint contract (POST /open, GET /approval/{ref},
# POST /close). Swap this Deployment's image for snow/jira later — same Service
# name, same CFOP_EXEC_CHANGE_URL. Stdlib-only at runtime.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 changerecord
USER changerecord
WORKDIR /home/changerecord

COPY --chown=changerecord:changerecord shapes.py /home/changerecord/shapes.py
COPY --chown=changerecord:changerecord github_client.py /home/changerecord/github_client.py
COPY --chown=changerecord:changerecord github_recorder.py /home/changerecord/github_recorder.py
COPY --chown=changerecord:changerecord entrypoint.py /home/changerecord/entrypoint.py

ENV CFOP_CHANGERECORD_HOST=0.0.0.0
ENV CFOP_CHANGERECORD_PORT=8091
EXPOSE 8091

ENTRYPOINT ["python3", "/home/changerecord/entrypoint.py"]
