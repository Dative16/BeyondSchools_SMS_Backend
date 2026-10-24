# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /srv
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

FROM base AS deps
COPY pyproject.toml ./
# Some networks re-sign TLS connections (corporate proxies, antivirus HTTPS
# scanning), which leaves pip unable to verify pypi.org from inside a build.
# Mounting that network's root CA fixes it properly; the alternative,
# --trusted-host, installs dependencies with no verification at all, which is
# not a trade worth making for a service that holds children's records.
# See docker/extra-ca.pem and EXTRA_CA_FILE in docker-compose.yml.
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then \
      cat /run/secrets/extra_ca >> /etc/ssl/certs/ca-certificates.crt; \
    fi \
 && pip install --upgrade pip && pip install ".[dev]" \
 # httpx verifies against certifi's bundle rather than the system one, so the
 # same CA has to go there too — otherwise every outbound call (the SMS
 # gateway, the payment aggregator, the AI platform) fails verification at
 # runtime even though the build succeeded.
 && if [ -s /run/secrets/extra_ca ]; then \
      cat /run/secrets/extra_ca >> "$(python -c 'import certifi; print(certifi.where())')"; \
    fi

FROM deps AS dev
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM base AS runtime
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY . .
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /srv
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
