FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY capability.proto .
RUN python -m grpc_tools.protoc \
    --proto_path=. \
    --python_out=. \
    --grpc_python_out=. \
    capability.proto

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    git \
    ripgrep \
    ca-certificates \
    curl \
    gzip \
    nodejs \
    npm \
    golang-go \
    openjdk-17-jre-headless \
    unzip \
    && rm -rf /var/lib/apt/lists/*

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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN npm install -g \
    typescript \
    typescript-language-server \
    jdtls \
    kotlin-language-server \
    bash-language-server \
    yaml-language-server

RUN go install golang.org/x/tools/gopls@latest \
    && ln -sf "$(go env GOPATH)/bin/gopls" /usr/local/bin/gopls

COPY --from=builder /build/capability_pb2.py /build/capability_pb2_grpc.py ./
COPY server.py ./

RUN id -u 1000 2>/dev/null || useradd --uid 1000 --create-home selu

RUN mkdir -p /workspace && chown -R 1000:1000 /workspace
VOLUME /workspace

EXPOSE 50051
USER 1000:1000

CMD ["python", "server.py"]
