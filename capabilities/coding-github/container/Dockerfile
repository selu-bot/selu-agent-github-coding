# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt
COPY capability.proto .
RUN python -m grpc_tools.protoc \
    --proto_path=. \
    --python_out=. \
    --grpc_python_out=. \
    capability.proto

FROM python:3.12-slim

WORKDIR /app

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
    git \
    ripgrep \
    ca-certificates \
    curl \
    gzip \
    nodejs \
    npm \
    golang-go \
    default-jre-headless \
    unzip

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) ra_arch="x86_64-unknown-linux-gnu" ;; \
      arm64) ra_arch="aarch64-unknown-linux-gnu" ;; \
      *) echo "Unsupported architecture: ${arch}"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/rust-lang/rust-analyzer/releases/latest/download/rust-analyzer-${ra_arch}.gz" -o /tmp/rust-analyzer.gz; \
    gunzip /tmp/rust-analyzer.gz; \
    install -m 0755 /tmp/rust-analyzer /usr/local/bin/rust-analyzer; \
    rm -f /tmp/rust-analyzer; \
    rust-analyzer --version

RUN set -eux; \
    curl -fsSL "https://download.eclipse.org/jdtls/snapshots/jdt-language-server-latest.tar.gz" -o /tmp/jdtls.tar.gz; \
    mkdir -p /opt/jdtls; \
    tar -xzf /tmp/jdtls.tar.gz -C /opt/jdtls; \
    ln -sf /opt/jdtls/bin/jdtls /usr/local/bin/jdtls; \
    rm -f /tmp/jdtls.tar.gz; \
    test -x /usr/local/bin/jdtls

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "${arch}" in \
      amd64) klsp_arch="x64" ;; \
      arm64) klsp_arch="aarch64" ;; \
      *) echo "Unsupported architecture: ${arch}"; exit 1 ;; \
    esac; \
    release_json="$(curl -fsSL https://api.github.com/repos/Kotlin/kotlin-lsp/releases/latest)"; \
    klsp_url="$(printf '%s' "${release_json}" | sed -n 's/.*\(https:\/\/download-cdn\.jetbrains\.com\/kotlin-lsp\/[^"]*linux-'"${klsp_arch}"'\.zip\).*/\1/p' | head -n 1)"; \
    if [ -z "${klsp_url}" ]; then echo "Could not resolve Kotlin LSP download URL"; exit 1; fi; \
    curl -fsSL "${klsp_url}" -o /tmp/kotlin-lsp.zip; \
    mkdir -p /opt/kotlin-lsp; \
    unzip -q /tmp/kotlin-lsp.zip -d /opt/kotlin-lsp; \
    chmod +x /opt/kotlin-lsp/kotlin-lsp.sh; \
    ln -sf /opt/kotlin-lsp/kotlin-lsp.sh /usr/local/bin/kotlin-language-server; \
    rm -f /tmp/kotlin-lsp.zip; \
    timeout 5s kotlin-language-server --version || true

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

RUN --mount=type=cache,target=/root/.npm \
    npm install -g \
    typescript \
    typescript-language-server \
    bash-language-server \
    yaml-language-server

RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    go install golang.org/x/tools/gopls@latest \
    && install -m 0755 "$(go env GOPATH)/bin/gopls" /usr/local/bin/gopls

COPY --from=builder /build/capability_pb2.py /build/capability_pb2_grpc.py ./
COPY server.py ./

RUN id -u 1000 2>/dev/null || useradd --uid 1000 --create-home selu

RUN mkdir -p /workspace && chown -R 1000:1000 /workspace
VOLUME /workspace

EXPOSE 50051
USER 1000:1000

CMD ["python", "server.py"]
