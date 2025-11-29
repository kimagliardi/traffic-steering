#!/usr/bin/env bash
# Enable Prometheus pod annotations for all deployments in a namespace
# Usage: ./enable_metrics.sh <namespace> <metrics_port> <metrics_path>
set -euo pipefail
ns=${1:-free5gc}
port=${2:-8080}
path=${3:-/metrics}
echo "Enabling prometheus pod annotations in namespace: $ns (port=$port path=$path)"
deps=$(kubectl -n "$ns" get deployments -o jsonpath='{range .items[*]}{.metadata.name}"\n"{end}')
for d in $deps; do
  echo "Patching deployment: $d"
  kubectl -n "$ns" patch deployment "$d" --type='json' -p "[
    {\"op\": \"add\", \"path\": \"/spec/template/metadata/annotations/prometheus.io~1scrape\", \"value\": \"true\"},
    {\"op\": \"add\", \"path\": \"/spec/template/metadata/annotations/prometheus.io~1port\", \"value\": \"$port\"},
    {\"op\": \"add\", \"path\": \"/spec/template/metadata/annotations/prometheus.io~1path\", \"value\": \"$path\"}
  ]" || echo "Patch failed for $d (it may already have annotations)"
done
echo "Done."
