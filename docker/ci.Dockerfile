# The CI runner, locally: the Python CI and production use, with requirements
# installed. Built and run by scripts/ci_local.py; the source is mounted at run
# time, so only a requirements change rebuilds this layer.
FROM python:3.11-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /src
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
