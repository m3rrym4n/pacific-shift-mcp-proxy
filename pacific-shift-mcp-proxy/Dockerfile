ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.19
FROM ${BUILD_FROM}

COPY rootfs /
COPY pacific_shift_mcp_proxy /opt/pacific_shift_mcp_proxy
COPY start.py /usr/bin/start.py

RUN chmod a+x /usr/bin/start.py
RUN chmod a+x /etc/services.d/mcp-proxy/run
RUN chmod a+x /etc/services.d/mcp-proxy/finish
