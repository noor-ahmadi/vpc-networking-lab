FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 iputils-ping iputils-arping tcpdump procps util-linux nftables shellcheck \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
CMD ["bash", "tests/integration.sh"]
