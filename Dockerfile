FROM python:3.12-slim

RUN useradd --create-home --uid 10001 nexus
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"

USER nexus
WORKDIR /data
EXPOSE 8402
# Mount a volume on /data for the identity key, L402 secret and ledger, and
# provide services.json there. Configure the Lightning backend with NEXUS_LN_BACKEND etc.
CMD ["aibtc-nexus", "serve-provider", "--config", "/data/services.json", \
     "--identity", "/data/provider.key", "--db", "/data/provider.db", \
     "--l402-secret-file", "/data/l402.secret", "--host", "0.0.0.0", "--port", "8402"]
