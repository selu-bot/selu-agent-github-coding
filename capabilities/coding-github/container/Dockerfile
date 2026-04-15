# syntax=docker/dockerfile:1.7
ARG BASE_IMAGE=python:3.12-slim
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

FROM ${BASE_IMAGE}

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY --from=builder /build/capability_pb2.py /build/capability_pb2_grpc.py ./
COPY server.py ./
RUN id -u 1000 2>/dev/null || useradd --uid 1000 --create-home selu
RUN mkdir -p /workspace /cache && chown -R 1000:1000 /workspace /cache
VOLUME /workspace

# Redirect package-manager caches to the persistent /cache volume so that
# downloaded dependencies survive across sessions (not just container restarts).
# The /cache volume is user-scoped and managed via the web UI.
# Toolchain binaries (cargo, rustc, go, node) remain on the read-only rootfs
# via PATH; only the download/registry caches move to the cache volume.
ENV CARGO_HOME=/cache/cargo \
    GOPATH=/cache/go \
    GOMODCACHE=/cache/go/pkg/mod \
    npm_config_cache=/cache/npm \
    PIP_CACHE_DIR=/cache/pip \
    XDG_CACHE_HOME=/cache

EXPOSE 50051
USER 1000:1000

CMD ["python", "server.py"]
