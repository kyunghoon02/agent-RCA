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
COPY config/online-boutique-krca.yaml /app/config/online-boutique-krca.yaml
COPY tools/run_stategraph_reconciler.py /app/tools/run_stategraph_reconciler.py
COPY tools/run_incident_receiver.py /app/tools/run_incident_receiver.py
COPY tools/run_incident_worker.py /app/tools/run_incident_worker.py

RUN chown -R 65532:65532 /app

USER 65532:65532

ENTRYPOINT ["python", "/app/tools/run_stategraph_reconciler.py"]
