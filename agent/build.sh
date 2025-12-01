#!/bin/bash
# Build and deploy the Traffic Steering LLM Agent to Kubernetes

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="${SCRIPT_DIR}"
IMAGE_NAME="${IMAGE_NAME:-traffic-steering-agent}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
REGISTRY="${REGISTRY:-}"  # e.g., "localhost:32000" for MicroK8s

echo "🔧 Building Traffic Steering Agent"
echo "=================================="

# Build Docker image
echo "📦 Building Docker image: ${IMAGE_NAME}:${IMAGE_TAG}"

cd "${AGENT_DIR}"

docker build -f Dockerfile.llm -t "${IMAGE_NAME}:${IMAGE_TAG}" .

# Tag for registry if specified
if [ -n "${REGISTRY}" ]; then
    FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
    echo "🏷️  Tagging image: ${FULL_IMAGE}"
    docker tag "${IMAGE_NAME}:${IMAGE_TAG}" "${FULL_IMAGE}"
    
    echo "📤 Pushing image to registry..."
    docker push "${FULL_IMAGE}"
    
    # Update deployment with registry image
    sed -i "s|image: traffic-steering-agent:latest|image: ${FULL_IMAGE}|g" k8s/deployment.yaml
fi

echo ""
echo "✅ Build complete!"
echo ""
echo "To deploy to Kubernetes:"
echo "  kubectl apply -f ${AGENT_DIR}/k8s/deployment.yaml"
echo ""
echo "Or for MicroK8s:"
echo "  microk8s kubectl apply -f ${AGENT_DIR}/k8s/deployment.yaml"
