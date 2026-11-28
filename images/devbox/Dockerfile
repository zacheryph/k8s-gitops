# Devbox: a general-purpose workspace image that runs bb (getbb.app) by
# default. Runtimes and CLIs baked in here live under /usr/local and are
# replaced on every image bump; anything installed at runtime goes into
# /home/dev (a PVC in the cluster) and survives restarts. See README.md.
FROM ubuntu:26.04@sha256:d66b366dac61b575bee2aef07e9c26e8070096fbe84a3d4d3eb0da069b5d76db

ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# System packages. Build tooling stays in so native npm/pip/gem modules compile.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash-completion build-essential ca-certificates curl dnsutils file fzf \
      git git-lfs gnupg htop iproute2 iputils-ping jq less libssl-dev lsof \
      make man-db nano netcat-openbsd openssh-client pkg-config postgresql-client \
      procps psmisc python3 python3-dev python3-pip python3-venv ripgrep rsync \
      shellcheck sqlite3 sudo tini tmux tree tzdata unzip vim wget xz-utils zip zstd \
      fd-find \
 && ln -s /usr/bin/fdfind /usr/local/bin/fd \
 && rm -rf /var/lib/apt/lists/*

# Node.js (bb supports 22.19, 24 and 26; Pi needs 24+). Node 25+ no longer
# bundles corepack (pnpm/yarn shims), so it comes from npm.
# renovate: datasource=node-version depName=node versioning=node
ARG NODE_VERSION=26.13.1
# renovate: datasource=npm depName=corepack
ARG COREPACK_VERSION=0.36.0
RUN case "${TARGETARCH:-amd64}" in amd64) a=x64;; arm64) a=arm64;; esac \
 && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${a}.tar.xz" \
    | tar -xJ -C /usr/local --strip-components=1 --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md \
 && npm install -g --no-fund --no-audit "corepack@${COREPACK_VERSION}" \
 && corepack enable \
 && node --version && corepack --version

# Bun
# renovate: datasource=github-releases depName=oven-sh/bun extractVersion=^bun-v(?<version>.+)$
ARG BUN_VERSION=1.4.5
RUN case "${TARGETARCH:-amd64}" in amd64) a=x64;; arm64) a=aarch64;; esac \
 && curl -fsSLo /tmp/bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-${a}.zip" \
 && unzip -j /tmp/bun.zip -d /usr/local/bin \
 && ln -s bun /usr/local/bin/bunx \
 && rm /tmp/bun.zip \
 && bun --version

# Go
# renovate: datasource=golang-version depName=go
ARG GO_VERSION=1.27.4
RUN curl -fsSL "https://dl.google.com/go/go${GO_VERSION}.linux-${TARGETARCH:-amd64}.tar.gz" \
    | tar -xz -C /usr/local \
 && /usr/local/go/bin/go version

# uv (Python toolchains and tools)
# renovate: datasource=github-releases depName=astral-sh/uv
ARG UV_VERSION=0.13.10
RUN case "${TARGETARCH:-amd64}" in amd64) a=x86_64;; arm64) a=aarch64;; esac \
 && curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${a}-unknown-linux-gnu.tar.gz" \
    | tar -xz -C /usr/local/bin --strip-components=1 \
 && uv --version

# mise: install any other language or CLI into $HOME at runtime
# renovate: datasource=github-releases depName=jdx/mise
ARG MISE_VERSION=2026.11.14
RUN case "${TARGETARCH:-amd64}" in amd64) a=x64;; arm64) a=arm64;; esac \
 && curl -fsSLo /usr/local/bin/mise "https://github.com/jdx/mise/releases/download/v${MISE_VERSION}/mise-v${MISE_VERSION}-linux-${a}" \
 && chmod +x /usr/local/bin/mise \
 && mise --version

# GitHub CLI
# renovate: datasource=github-releases depName=cli/cli
ARG GH_VERSION=2.106.0
RUN curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${TARGETARCH:-amd64}.tar.gz" \
    | tar -xz -C /usr/local --strip-components=1 --wildcards '*/bin/gh' '*/share/man' \
 && gh --version

# Cluster tooling: kubectl, helm, flux, sops, yq
# renovate: datasource=github-releases depName=kubernetes/kubernetes
ARG KUBECTL_VERSION=1.37.3
# renovate: datasource=github-releases depName=helm/helm
ARG HELM_VERSION=4.3.2
# renovate: datasource=github-releases depName=fluxcd/flux2
ARG FLUX_VERSION=2.10.1
# renovate: datasource=github-releases depName=getsops/sops
ARG SOPS_VERSION=3.13.3
# renovate: datasource=github-releases depName=mikefarah/yq
ARG YQ_VERSION=4.54.3
RUN arch="${TARGETARCH:-amd64}" \
 && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/${arch}/kubectl" \
 && curl -fsSL "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${arch}.tar.gz" \
    | tar -xz -C /usr/local/bin --strip-components=1 "linux-${arch}/helm" \
 && curl -fsSL "https://github.com/fluxcd/flux2/releases/download/v${FLUX_VERSION}/flux_${FLUX_VERSION}_linux_${arch}.tar.gz" \
    | tar -xz -C /usr/local/bin flux \
 && curl -fsSLo /usr/local/bin/sops "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.${arch}" \
 && curl -fsSLo /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_${arch}" \
 && chmod +x /usr/local/bin/kubectl /usr/local/bin/sops /usr/local/bin/yq \
 && kubectl version --client && helm version --short && flux --version && sops --version && yq --version

# bb and the agent CLIs it drives. These are the image's copies; a newer one
# installed with `npm i -g` at runtime lands in ~/.local and takes precedence.
# renovate: datasource=npm depName=bb-app
ARG BB_VERSION=0.50.1
# renovate: datasource=npm depName=@earendil-works/pi-coding-agent
ARG PI_VERSION=1.7.0
# renovate: datasource=npm depName=@anthropic-ai/claude-code
ARG CLAUDE_CODE_VERSION=2.1.332
# renovate: datasource=npm depName=@openai/codex
ARG CODEX_VERSION=0.172.0
RUN npm install -g --no-fund --no-audit \
      "bb-app@${BB_VERSION}" \
      "@earendil-works/pi-coding-agent@${PI_VERSION}" \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force \
 && bb-app --help >/dev/null && pi --version && claude --version && codex --version

# The `dev` user (uid/gid 1000) owns /home/dev, which is the persistent volume.
RUN userdel -r ubuntu 2>/dev/null || true \
 && groupadd -g 1000 dev \
 && useradd -m -u 1000 -g 1000 -s /bin/bash dev

COPY rootfs/ /
RUN chmod 0440 /etc/sudoers.d/dev \
 && chmod 0755 /usr/local/bin/devbox-entrypoint /usr/local/bin/devbox-apt \
 && cp -a /etc/skel/.config /home/dev/ && chown -R dev:dev /home/dev

# Runtime installs go to $HOME, ahead of the image's copies on PATH.
ENV HOME=/home/dev \
    USER=dev \
    NPM_CONFIG_PREFIX=/home/dev/.local \
    BUN_INSTALL=/home/dev/.bun \
    GOPATH=/home/dev/go \
    MISE_YES=1 \
    PATH=/home/dev/.local/share/mise/shims:/home/dev/.local/bin:/home/dev/.bun/bin:/home/dev/.cargo/bin:/home/dev/go/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games \
    BB_TELEMETRY=false

WORKDIR /home/dev
EXPOSE 38886

# The entrypoint starts as root to reinstall persisted apt packages, then
# drops to `dev`. bb binds 0.0.0.0 here: its API is unauthenticated, so the
# port must only ever be reachable through an authenticating proxy.
# bb runs on the image's node, not whatever `node` mise puts first on PATH:
# its native modules (better-sqlite3, node-pty) are built for this one.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/devbox-entrypoint"]
CMD ["/usr/local/bin/node", "/usr/local/lib/node_modules/bb-app/dist/bb-app.js", "--data-dir", "/home/dev/.bb", "--server-bind-host", "0.0.0.0", "--server-port", "38886", "--no-in-app-updates", "--bundled"]
