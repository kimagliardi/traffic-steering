# Traffic Steering LLM Agent - Kubernetes Deployment

This directory contains everything needed to deploy the LLM-powered Traffic Steering Agent to Kubernetes.

## Prerequisites

1. **Kubernetes cluster** (MicroK8s, k3s, or full K8s)
2. **Ollama** running with `qwen2.5-coder` model (can deploy in-cluster or use external)
3. **SSH access** to UERANSIM VM from the cluster nodes
4. **free5GC** deployed with ULCL enabled

## Files

| File | Description |
|------|-------------|
| `Dockerfile.llm` | Docker image for the agent |
| `requirements-llm.txt` | Python dependencies |
| `traffic_steering_llm_agent_k8s.py` | Main agent code (Kubernetes version) |
| `k8s/deployment.yaml` | Kubernetes deployment manifests |
| `k8s/ollama.yaml` | Optional Ollama deployment |
| `build.sh` | Build and deploy script |

## Quick Start

### 1. Deploy Ollama (if not already running)

```bash
# Deploy Ollama to the cluster
kubectl apply -f k8s/ollama.yaml

# Wait for it to be ready
kubectl wait --for=condition=ready pod -l app=ollama -n ollama --timeout=300s

# The Job will automatically pull qwen2.5-coder model
```

### 2. Configure SSH Key for UERANSIM

Create a secret with the SSH key to access UERANSIM VM:

```bash
# Using existing Vagrant key
kubectl create secret generic traffic-steering-agent-ssh \
  -n free5gc \
  --from-file=ssh-private-key=/path/to/vagrant/private_key

# Or generate a new key pair and add public key to UERANSIM VM
```

### 3. Build and Deploy the Agent

```bash
# Build the Docker image
cd agent
chmod +x build.sh
./build.sh

# For MicroK8s with built-in registry
REGISTRY=localhost:32000 ./build.sh

# Deploy to Kubernetes
kubectl apply -f k8s/deployment.yaml
```

### 4. Check Deployment Status

```bash
# Check pod status
kubectl get pods -n free5gc -l app=traffic-steering-agent

# Check logs
kubectl logs -n free5gc -l app=traffic-steering-agent -f
```

## Configuration

Configuration is done via ConfigMap. Edit `k8s/deployment.yaml` to change:

| Variable | Default | Description |
|----------|---------|-------------|
| `NEF_URL` | `http://free5gc-v1-free5gc-nef-nef-sbi:80` | NEF service URL |
| `PROMETHEUS_URL` | `http://prometheus-...:9090` | Prometheus URL |
| `OLLAMA_API_BASE` | `http://ollama.ollama:11434` | Ollama API URL |
| `LLM_MODEL` | `qwen2.5-coder` | LLM model to use |
| `UERANSIM_HOST` | `192.168.56.118` | UERANSIM VM IP |

## Interacting with the Agent

The agent runs in interactive mode by default. To interact:

```bash
# Attach to the agent container
kubectl exec -it -n free5gc deploy/traffic-steering-agent -- python traffic_steering_llm_agent_k8s.py

# Or run one-off commands
kubectl exec -n free5gc deploy/traffic-steering-agent -- python -c "
from traffic_steering_llm_agent_k8s import TrafficSteeringAgent
agent = TrafficSteeringAgent()
print(agent.process_request('Check system health'))
"
```

## Health Checks

The agent exposes health endpoints on port 8080:

- `/health` - Liveness check
- `/ready` - Readiness check

```bash
# Port-forward to test
kubectl port-forward -n free5gc deploy/traffic-steering-agent 8080:8080
curl http://localhost:8080/health
```

## Troubleshooting

### Agent can't connect to NEF

```bash
# Check NEF service
kubectl get svc -n free5gc | grep nef

# Test connectivity from agent pod
kubectl exec -n free5gc deploy/traffic-steering-agent -- \
  curl -s http://free5gc-v1-free5gc-nef-nef-sbi:80/3gpp-traffic-influence/v1/test/subscriptions
```

### Agent can't SSH to UERANSIM

```bash
# Check SSH key is mounted
kubectl exec -n free5gc deploy/traffic-steering-agent -- ls -la /app/ssh/

# Test SSH connection
kubectl exec -n free5gc deploy/traffic-steering-agent -- \
  ssh -o StrictHostKeyChecking=no -i /app/ssh/ssh-private-key vagrant@192.168.56.118 "echo success"
```

### LLM not responding

```bash
# Check Ollama is running
kubectl get pods -n ollama

# Test Ollama API
kubectl exec -n free5gc deploy/traffic-steering-agent -- \
  curl -s http://ollama.ollama:11434/api/tags
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Kubernetes Cluster                          │
│                                                                 │
│  ┌─────────────────┐    ┌─────────────────┐                    │
│  │  Traffic        │    │     Ollama      │                    │
│  │  Steering       │───▶│  (qwen2.5-coder)│                    │
│  │  Agent          │    └─────────────────┘                    │
│  │                 │                                            │
│  │  - Tools        │    ┌─────────────────┐                    │
│  │  - LLM Agent    │───▶│   free5GC       │                    │
│  │  - K8s Client   │    │   (NEF, SMF,    │                    │
│  └────────┬────────┘    │    UPFs...)     │                    │
│           │             └─────────────────┘                    │
│           │                                                     │
│           │             ┌─────────────────┐                    │
│           │             │   Prometheus    │                    │
│           └────────────▶│   (metrics)     │                    │
│                         └─────────────────┘                    │
└───────────────────────────────┬─────────────────────────────────┘
                                │ SSH
                                ▼
                    ┌─────────────────────┐
                    │    UERANSIM VM      │
                    │  (192.168.56.118)   │
                    └─────────────────────┘
```
