FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# Run as an unprivileged user. /data holds the volumes (SQLite, second brain);
# owning it in the image makes new named volumes writable by that user.
RUN useradd --create-home --uid 10001 uva \
    && mkdir -p /data/sqlite /data/second-brain \
    && chown -R uva:uva /data
USER uva

CMD ["python", "-m", "uva.main"]
