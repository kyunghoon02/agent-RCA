FROM docker.io/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --requirement /app/requirements.txt

COPY src /app/src
COPY contracts /app/contracts
COPY db /app/db
COPY knowledge /app/knowledge
COPY config/online-boutique-krca.yaml /app/config/online-boutique-krca.yaml
COPY tools/run_stategraph_reconciler.py /app/tools/run_stategraph_reconciler.py
COPY tools/run_incident_receiver.py /app/tools/run_incident_receiver.py
COPY tools/run_incident_worker.py /app/tools/run_incident_worker.py
COPY tools/run_agent_worker.py /app/tools/run_agent_worker.py
COPY tools/run_incident_viewer.py /app/tools/run_incident_viewer.py

ADD https://github.com/cilium/hubble/releases/download/v1.19.4/hubble-linux-amd64.tar.gz /tmp/hubble-linux-amd64.tar.gz
RUN echo "c7c37a5f27ed2ae2d5495c6ddbecc61cebbb841508e77af3c131e96f005ed247  /tmp/hubble-linux-amd64.tar.gz" \
      | sha256sum --check --strict \
    && tar --extract --gzip --file /tmp/hubble-linux-amd64.tar.gz \
      --directory /usr/local/bin hubble \
    && chmod 0755 /usr/local/bin/hubble \
    && rm /tmp/hubble-linux-amd64.tar.gz

RUN chown -R 65532:65532 /app

USER 65532:65532

ENTRYPOINT ["python", "/app/tools/run_stategraph_reconciler.py"]
