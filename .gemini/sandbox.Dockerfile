FROM us-docker.pkg.dev/gemini-code-dev/gemini-cli/sandbox:0.2.1

USER root
RUN apt-get update && \
    apt-get install -y python3 python3-pip python3-venv curl && \
    rm -rf /var/lib/apt/lists/*

RUN export PATH="$PATH:/root/.local/bin" && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    uv venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

USER node
