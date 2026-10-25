# This is separate from the working LiteLLM image and its Python/Prisma stack.
FROM node:22-bookworm-slim AS gmail
RUN npm install --prefix /opt/gmail --omit=dev --no-audit --no-fund @shinzolabs/gmail-mcp@1.7.4 && \
    node -e 'const fs=require("fs"),path=require("path");const base="/opt/gmail/node_modules/@shinzolabs/gmail-mcp";const p=require(base+"/package.json");const b=typeof p.bin==="string"?p.bin:Object.values(p.bin||{})[0];if(!b)throw Error("Gmail package has no executable");fs.symlinkSync(path.resolve(base,b),"/opt/gmail/entry.js");'

FROM rust:1-bookworm AS linkedin
ARG LINKEDIN_RS_REF=main
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
RUN git init /src/linkedin-rs && git -C /src/linkedin-rs remote add origin https://github.com/olgasafonova/linkedin-rs.git && \
    git -C /src/linkedin-rs fetch --depth=1 origin "${LINKEDIN_RS_REF}" && git -C /src/linkedin-rs checkout --detach FETCH_HEAD
COPY docs/guides/source-tools/build_linkedin.py /build_linkedin.py
RUN python3 /build_linkedin.py

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright TELEMETRY_ENABLED=false
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates coreutils libstdc++6 libssl3 && rm -rf /var/lib/apt/lists/*
COPY --from=gmail /usr/local/bin/node /usr/local/bin/node
COPY --from=gmail /opt/gmail /opt/gmail
COPY --from=linkedin /out/li /usr/local/bin/li
COPY --from=linkedin /out/linkedin-rs-revision.txt /opt/applypilot/linkedin-rs-revision.txt
COPY docs/guides/source-tools/install_sdks.py /opt/applypilot/install_sdks.py
RUN python /opt/applypilot/install_sdks.py && \
    /opt/tools/playwright/bin/python -m playwright install --with-deps chromium && \
    /opt/tools/linkedin_scraper/bin/python -m playwright install --with-deps chromium && \
    chmod -R a+rX /opt/tools /ms-playwright && rm -rf /var/lib/apt/lists/*
COPY src/applypilot/integrations/worker_main.py /opt/applypilot/worker_main.py
COPY docs/guides/source-tools/smoke.py /opt/applypilot/smoke.py
RUN --network=none python /opt/applypilot/smoke.py
LABEL org.applypilot.runtime="source-tools-v1"
# No TCP API and no Docker socket. Fixed commands are invoked by the local app.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD test -s /opt/applypilot/sdk-versions.json && test -x /usr/local/bin/li && test -e /opt/gmail/entry.js
CMD ["sleep", "infinity"]
