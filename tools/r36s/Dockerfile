# reVC build image for R36S (aarch64, Ubuntu 20.04 / glibc 2.31) - see docs/02
# Same base as the official PortMaster builder (arm64v8/ubuntu:20.04), but only
# the packages reVC needs. Base image via daocloud mirror, apt via aliyun
# ubuntu-ports (ghcr.io / ports.ubuntu.com are too slow from here).
ARG BASE=docker.m.daocloud.io/arm64v8/ubuntu:20.04
FROM ${BASE}
ARG APT_MIRROR=http://mirrors.aliyun.com/ubuntu-ports
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i "s|http://ports.ubuntu.com/ubuntu-ports|${APT_MIRROR}|g" /etc/apt/sources.list \
 && apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build clang ccache git pkg-config file binutils \
        libsdl2-dev libopenal-dev libmpg123-dev \
        libegl1-mesa-dev libgles2-mesa-dev \
 && rm -rf /var/lib/apt/lists/*
