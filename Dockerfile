# One image, reused by every Python service. Service identity comes from the
# command in compose.yaml, not from separate images.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Install into the system prefix rather than /app/.venv, so `python -m ...`
    # is the same command inside the container as it is on the host.
    UV_PROJECT_ENVIRONMENT=/usr/local

# Unpinned deliberately for now -- pin once you have seen which version installs.
RUN pip install --no-cache-dir uv

WORKDIR /app

# Two-step sync so a source edit does not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY src ./src
RUN uv sync --frozen

# Overridden per service. The default is the stage 2 verification itself.
CMD ["python", "-c", "from telemetry.config import settings; print(settings.describe())"]
