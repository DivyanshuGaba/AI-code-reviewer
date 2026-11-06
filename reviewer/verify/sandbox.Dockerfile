# Image used to run checks on patched copies of a repository.
# Built once with `reviewer sandbox-build` (needs network); runs with no network.
FROM python:3.12-slim
RUN pip install --no-cache-dir ruff mypy pytest \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin sandbox
USER 10001
WORKDIR /work
