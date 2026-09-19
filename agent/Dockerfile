FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN addgroup --system aiscelapeus && adduser --system --ingroup aiscelapeus aiscelapeus

COPY agent/pyproject.toml ./pyproject.toml
COPY agent/aiscelapeus ./aiscelapeus

RUN python -m pip install --no-cache-dir . \
    && python -m aiscelapeus.main download-files \
    && chown -R aiscelapeus:aiscelapeus /app

USER aiscelapeus

EXPOSE 8081

CMD ["python", "-m", "aiscelapeus.main", "start"]
