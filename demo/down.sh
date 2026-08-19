#!/usr/bin/env bash
# CFOP-31: tear the demo down. Order matters — compose first (it holds a
# connection into kind's docker network; deleting the cluster first leaves
# the network with active endpoints), then the cluster, then local state.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

docker compose -f docker-compose.yml -f demo/docker-compose.demo.yml down -v || true
kind delete cluster --name cfop-demo || true
rm -rf demo/.kube
printf '\ndemo torn down\n'
