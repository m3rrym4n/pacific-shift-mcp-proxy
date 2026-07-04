ARG BUILD_FROM=ghcr.io/hassio-addons/base:latest
FROM ${BUILD_FROM}

RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-aiohttp

RUN pip3 install --break-system-packages \
    aiohttp

COPY rootfs /

RUN chmod a+x /etc/services.d/mcp-proxy/run
RUN chmod a+x /etc/services.d/mcp-proxy/finish
