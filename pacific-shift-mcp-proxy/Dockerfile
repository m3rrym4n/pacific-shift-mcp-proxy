ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.19
FROM ${BUILD_FROM}

RUN pip3 install --break-system-packages \
    aiohttp

COPY rootfs /

RUN chmod a+x /etc/services.d/mcp-proxy/run
RUN chmod a+x /etc/services.d/mcp-proxy/finish
