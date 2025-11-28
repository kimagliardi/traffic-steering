import os
from prometheus_client import Counter, Histogram

# --- CONFIGURATION ---
# Environment variables for K8s flexibility
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-server.free5gc.svc.cluster.local:80")
NEF_URL = os.getenv("NEF_URL", "http://nef-service.free5gc.svc.cluster.local:8000")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))

# IDs
AF_ID = "SelfHealingAgent"
SUBSCRIPTION_ID = "sub_auto_01"

# Network Topology
CENTRAL_UPF_IP = os.getenv("CENTRAL_UPF_IP", "192.168.56.101")
EDGE_UPF_IP    = os.getenv("EDGE_UPF_IP", "192.168.56.120")

# Metric Query - configurable device name
# For UERANSIM: use uesimtun0 (created on UE host machine, not in K8s)
# Note: uesimtun0 only exists on the host where UERANSIM UE is running
# If running UERANSIM outside K8s, ensure node-exporter runs on that host
# and Prometheus is configured to scrape it
NETWORK_DEVICE = os.getenv("NETWORK_DEVICE", "uesimtun0")
# You can also set PROMETHEUS_QUERY env var to override the entire query
METRIC_QUERY = os.getenv(
    "PROMETHEUS_QUERY",
    f'rate(node_network_receive_bytes_total{{device="{NETWORK_DEVICE}"}}[1m]) * 8 / 1000000'
) # Mbps

# --- PROMETHEUS METRICS (SLOs) ---
# Latency: Measures how long the LLM takes to make a decision
DECISION_LATENCY = Histogram(
    'agent_decision_latency_seconds', 
    'Time spent waiting for LLM inference',
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0]
)

# Counters: Measure success/failure rates
STEERING_EVENTS = Counter(
    'agent_steering_events_total', 
    'Total number of traffic steering actions triggered',
    ['target_route']
)

AGENT_ERRORS = Counter(
    'agent_errors_total', 
    'Total number of errors encountered',
    ['type'] # types: 'ai_parse', 'nef_api', 'prometheus_read'
)
