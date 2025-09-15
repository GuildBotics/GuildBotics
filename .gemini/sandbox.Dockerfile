FROM us-docker.pkg.dev/gemini-code-dev/gemini-cli/sandbox:0.2.1

USER root
RUN apt-get update && \
    apt-get install -y python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip && \
    /opt/venv/bin/pip install agno pyyaml types-PyYAML cryptography PyJWT croniter aiohttp python-dotenv emoji redis langcodes[data] python-i18n[YAML] google-genai pytest pytest-asyncio coverage pytest-cov

ENV PATH="/opt/venv/bin:$PATH"

USER node
