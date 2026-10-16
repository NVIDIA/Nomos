#!/bin/sh
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Generate a self-signed TLS cert on first start and exec the wrapped command
# with uvicorn SSL args appended. Existing certs in TLS_CERT_DIR are reused,
# so bind-mounting a real cert over /app/certs overrides the self-signed one.
#
# Env:
#   TLS_CERT_DIR   directory holding server.crt + server.key  (default /app/certs)
#   TLS_CERT_CN    CN + primary SAN for the generated cert    (default localhost)

set -eu

CERT_DIR="${TLS_CERT_DIR:-/app/certs}"
CERT_FILE="${TLS_CERT_FILE:-${CERT_DIR}/server.crt}"
KEY_FILE="${TLS_KEY_FILE:-${CERT_DIR}/server.key}"
CERT_CN="${TLS_CERT_CN:-localhost}"

mkdir -p "${CERT_DIR}"

if [ ! -f "${CERT_FILE}" ] || [ ! -f "${KEY_FILE}" ]; then
  echo "[tls-entrypoint] generating self-signed cert (CN=${CERT_CN}) at ${CERT_DIR}" >&2
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "${KEY_FILE}" \
    -out "${CERT_FILE}" \
    -days 365 \
    -subj "/CN=${CERT_CN}" \
    -addext "subjectAltName=DNS:${CERT_CN},DNS:localhost,IP:127.0.0.1" \
    >/dev/null 2>&1
  chmod 600 "${KEY_FILE}"
fi

exec "$@" --ssl-keyfile "${KEY_FILE}" --ssl-certfile "${CERT_FILE}"
