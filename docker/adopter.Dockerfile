# A clean machine, for testing what an adopter actually hits. The point is
# the ABSENCE of local state: no ~/.config/harbor, no crush, no systemd user
# session, no ssh keys, no uv cache.
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 \
    && rm -rf /var/lib/apt/lists/*

# A normal unprivileged user, because that is who installs a dev tool.
RUN useradd -m -s /bin/bash adopter
USER adopter
WORKDIR /home/adopter

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/home/adopter/.local/bin:${PATH}"

COPY --chown=adopter:adopter . /home/adopter/harbor
WORKDIR /home/adopter/harbor
