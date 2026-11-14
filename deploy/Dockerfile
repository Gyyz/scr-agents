# scr_agents application container: console, chat sessions, scanners, and the build/test sandbox.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ripgrep curl ca-certificates \
        build-essential cmake cppcheck \
    && rm -rf /var/lib/apt/lists/*

# semgrep rules are fetched at build time so scans do not need the registry at runtime
RUN git clone --depth 1 https://github.com/semgrep/semgrep-rules /opt/semgrep-rules \
    && rm -rf /opt/semgrep-rules/.git

WORKDIR /app
COPY pyproject.toml README.md ./
# src/ holds the scr_agents package, with its skills/
COPY src ./src
COPY mcp_servers ./mcp_servers
COPY deploy/scr_agents.yaml ./deploy/scr_agents.yaml
RUN pip install --no-cache-dir ".[agents,scanners]" semgrep

# Optional: the web search MCP server (mcp_servers/README.md). Needs Node; adds about 150 MB.
#   docker compose build --build-arg WITH_WEB_SEARCH=1
ARG WITH_WEB_SEARCH=0
RUN if [ "$WITH_WEB_SEARCH" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends nodejs npm && rm -rf /var/lib/apt/lists/* \
        && sh mcp_servers/web-search/install.sh; \
    fi

RUN useradd --create-home --uid 1000 scr-agents \
    && mkdir -p /data/out /data/projects && chown -R scr-agents:scr-agents /data /app
USER scr-agents
ENV HOME=/home/scr-agents PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8765

ENTRYPOINT ["scr-agents"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765", "--config", "/app/deploy/scr_agents.yaml"]
