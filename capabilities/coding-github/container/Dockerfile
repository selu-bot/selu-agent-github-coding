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
RUN mkdir -p /workspace && chown -R 1000:1000 /workspace
VOLUME /workspace

# Redirect package-manager caches to the persistent workspace volume so that
# downloaded dependencies survive container restarts within the same session.
# Toolchain binaries (cargo, rustc, go, node) remain on the read-only rootfs
# via PATH; only the download/registry caches move to writable storage.
ENV CARGO_HOME=/workspace/.cache/cargo \
    GOPATH=/workspace/.cache/go \
    GOMODCACHE=/workspace/.cache/go/pkg/mod \
    npm_config_cache=/workspace/.cache/npm \
    PIP_CACHE_DIR=/workspace/.cache/pip \
    XDG_CACHE_HOME=/workspace/.cache

EXPOSE 50051
USER 1000:1000

CMD ["python", "server.py"]
