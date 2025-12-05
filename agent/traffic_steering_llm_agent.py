#!/usr/bin/env python3
"""
Traffic Steering LLM Agent (Simplified)
=======================================
A simple AI-powered agent for 5G ULCL traffic steering with two core functions:
1. Query UPF network metrics from Prometheus
2. Steer traffic to edge1 or edge2 via NEF API

Includes Prometheus metrics for SLO monitoring:
- Request latency (response time)
- Request success/failure rate
- Request counts by endpoint
"""

import os
import json
import time
import threading
from dataclasses import dataclass
from functools import wraps

import requests
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from smolagents import Tool, CodeAgent, LiteLLMModel


# ============================================================================
# Prometheus Metrics for SLO
# ============================================================================

# Request latency histogram (for response time SLO)
REQUEST_LATENCY = Histogram(
    'traffic_steering_request_latency_seconds',
    'Request latency in seconds',
    ['endpoint', 'method'],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]
)

# Request counter (for success rate SLO)
REQUEST_TOTAL = Counter(
    'traffic_steering_requests_total',
    'Total number of requests',
    ['endpoint', 'method', 'status']
)

# Active requests gauge
ACTIVE_REQUESTS = Gauge(
    'traffic_steering_active_requests',
    'Number of currently active requests',
    ['endpoint']
)

# NEF API specific metrics
NEF_REQUESTS = Counter(
    'traffic_steering_nef_requests_total',
    'Total NEF API requests',
    ['operation', 'status']
)

NEF_LATENCY = Histogram(
    'traffic_steering_nef_latency_seconds',
    'NEF API latency in seconds',
    ['operation'],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
)

# Prometheus query metrics
PROMETHEUS_QUERIES = Counter(
    'traffic_steering_prometheus_queries_total',
    'Total Prometheus queries',
    ['status']
)

PROMETHEUS_LATENCY = Histogram(
    'traffic_steering_prometheus_latency_seconds',
    'Prometheus query latency in seconds',
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
)

# Steering operations
STEERING_OPERATIONS = Counter(
    'traffic_steering_operations_total',
    'Total steering operations',
    ['target', 'status']
)

# Current steering target (info metric)
CURRENT_TARGET = Gauge(
    'traffic_steering_current_target',
    'Current steering target (1=edge1, 2=edge2, 0=none)',
    []
)

# Auto-steering metrics
AUTO_STEER_TRIGGERS = Counter(
    'traffic_steering_auto_steer_triggers_total',
    'Auto-steering triggers',
    ['from_target', 'to_target', 'reason']
)

UPF_TRAFFIC_RATE = Gauge(
    'traffic_steering_upf_traffic_rate_bps',
    'Current traffic rate for each UPF in bytes/sec',
    ['upf']
)

AUTO_STEER_THRESHOLD = Gauge(
    'traffic_steering_auto_steer_threshold_bps',
    'Auto-steering threshold in bytes/sec',
    []
)


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class AgentConfig:
    """Configuration for the traffic steering agent"""
    # Network Function URLs
    nef_url: str = os.getenv("NEF_URL", "http://10.152.183.162:80")
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://prometheus-kube-prometheus-prometheus.monitoring:9090")
    
    # Traffic Steering
    af_id: str = os.getenv("AF_ID", "traffic-steering-agent")
    dnn: str = os.getenv("DNN", "internet")
    sst: int = int(os.getenv("SST", "1"))
    sd: str = os.getenv("SD", "010203")
    
    # LLM
    ollama_base: str = os.getenv("OLLAMA_API_BASE", "http://192.168.0.128:11434")
    model_name: str = os.getenv("LLM_MODEL", "qwen2.5-coder")
    
    # Auto-steering configuration
    auto_steer_enabled: bool = os.getenv("AUTO_STEER_ENABLED", "true").lower() == "true"
    auto_steer_interval: int = int(os.getenv("AUTO_STEER_INTERVAL", "30"))  # seconds
    auto_steer_threshold_bps: float = float(os.getenv("AUTO_STEER_THRESHOLD_BPS", "100000"))  # 100 KB/s default
    auto_steer_cooldown: int = int(os.getenv("AUTO_STEER_COOLDOWN", "60"))  # seconds between steers


CONFIG = AgentConfig()


# ============================================================================
# Tool 1: Get UPF Network Metrics from Prometheus
# ============================================================================

class GetUPFNetworkMetricsTool(Tool):
    """Tool to get network usage metrics for UPF pods from Prometheus"""
    
    name = "get_upf_network_metrics"
    description = """
    Query Prometheus to get network usage metrics for UPF (User Plane Function) pods.
    Returns a text table showing TX/RX bytes and rates per pod and interface.
    
    Use this tool when asked about:
    - UPF network usage
    - Traffic on UPF pods
    - Bandwidth consumption
    - Network statistics for free5gc UPFs
    """
    inputs = {}
    output_type = "string"
    
    def forward(self) -> str:
        """Query Prometheus for UPF network metrics and return as text table"""
        
        try:
            # Query for network bytes received by UPF pods
            rx_query = 'container_network_receive_bytes_total{namespace="free5gc",pod=~".*upf.*"}'
            tx_query = 'container_network_transmit_bytes_total{namespace="free5gc",pod=~".*upf.*"}'
            
            # Also get rate (bytes/sec over last 1 minute)
            rx_rate_query = 'rate(container_network_receive_bytes_total{namespace="free5gc",pod=~".*upf.*"}[1m])'
            tx_rate_query = 'rate(container_network_transmit_bytes_total{namespace="free5gc",pod=~".*upf.*"}[1m])'
            
            results = {}
            
            # Query RX bytes
            resp = requests.get(
                f"{CONFIG.prometheus_url}/api/v1/query",
                params={"query": rx_query},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                for result in data.get("data", {}).get("result", []):
                    pod = result["metric"].get("pod", "unknown")
                    interface = result["metric"].get("interface", "unknown")
                    key = (pod, interface)
                    if key not in results:
                        results[key] = {"pod": pod, "interface": interface}
                    results[key]["rx_bytes"] = float(result["value"][1])
            
            # Query TX bytes
            resp = requests.get(
                f"{CONFIG.prometheus_url}/api/v1/query",
                params={"query": tx_query},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                for result in data.get("data", {}).get("result", []):
                    pod = result["metric"].get("pod", "unknown")
                    interface = result["metric"].get("interface", "unknown")
                    key = (pod, interface)
                    if key not in results:
                        results[key] = {"pod": pod, "interface": interface}
                    results[key]["tx_bytes"] = float(result["value"][1])
            
            # Query RX rate
            resp = requests.get(
                f"{CONFIG.prometheus_url}/api/v1/query",
                params={"query": rx_rate_query},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                for result in data.get("data", {}).get("result", []):
                    pod = result["metric"].get("pod", "unknown")
                    interface = result["metric"].get("interface", "unknown")
                    key = (pod, interface)
                    if key in results:
                        results[key]["rx_rate_bps"] = float(result["value"][1])
            
            # Query TX rate
            resp = requests.get(
                f"{CONFIG.prometheus_url}/api/v1/query",
                params={"query": tx_rate_query},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                for result in data.get("data", {}).get("result", []):
                    pod = result["metric"].get("pod", "unknown")
                    interface = result["metric"].get("interface", "unknown")
                    key = (pod, interface)
                    if key in results:
                        results[key]["tx_rate_bps"] = float(result["value"][1])
            
            if not results:
                return "No UPF network metrics found in Prometheus."
            
            # Format as text table
            def format_bytes(b):
                if b >= 1e9:
                    return f"{b/1e9:.2f} GB"
                elif b >= 1e6:
                    return f"{b/1e6:.2f} MB"
                elif b >= 1e3:
                    return f"{b/1e3:.2f} KB"
                return f"{b:.0f} B"
            
            def format_rate(r):
                if r >= 1e6:
                    return f"{r*8/1e6:.2f} Mbps"
                elif r >= 1e3:
                    return f"{r*8/1e3:.2f} Kbps"
                return f"{r*8:.0f} bps"
            
            # Build table
            lines = []
            lines.append("=" * 100)
            lines.append("UPF Network Usage (from Prometheus)")
            lines.append("=" * 100)
            lines.append(f"{'POD':<45} {'INTERFACE':<12} {'RX TOTAL':<12} {'TX TOTAL':<12} {'RX RATE':<12} {'TX RATE':<12}")
            lines.append("-" * 100)
            
            # Sort by pod name
            sorted_results = sorted(results.values(), key=lambda x: (x["pod"], x["interface"]))
            
            for r in sorted_results:
                pod = r["pod"][:44]  # Truncate long names
                iface = r["interface"][:11]
                rx_bytes = format_bytes(r.get("rx_bytes", 0))
                tx_bytes = format_bytes(r.get("tx_bytes", 0))
                rx_rate = format_rate(r.get("rx_rate_bps", 0))
                tx_rate = format_rate(r.get("tx_rate_bps", 0))
                lines.append(f"{pod:<45} {iface:<12} {rx_bytes:<12} {tx_bytes:<12} {rx_rate:<12} {tx_rate:<12}")
            
            lines.append("=" * 100)
            
            return "\n".join(lines)
            
        except requests.exceptions.ConnectionError:
            return f"❌ Cannot connect to Prometheus at {CONFIG.prometheus_url}"
        except Exception as e:
            return f"❌ Error querying Prometheus: {str(e)}"


# ============================================================================
# Tool 2: Steer Traffic via NEF API
# ============================================================================

class SteerTrafficTool(Tool):
    """Tool to steer traffic to edge1 or edge2 via NEF Traffic Influence API"""
    
    name = "steer_traffic"
    description = """
    Steer UE traffic to a specific edge UPF by creating a NEF Traffic Influence subscription.
    
    - 'edge1' steers to AnchorUPF1 (IP pool: 10.1.0.0/17)
    - 'edge2' steers to AnchorUPF2 (IP pool: 10.1.128.0/17)
    
    This creates a subscription in the NEF that tells the SMF to route traffic through the specified UPF.
    """
    inputs = {
        "target": {
            "type": "string",
            "description": "Target DNAI: 'edge1' for AnchorUPF1 or 'edge2' for AnchorUPF2"
        }
    }
    output_type = "string"
    
    def forward(self, target: str) -> str:
        """Create traffic influence subscription via NEF API"""
        
        # Normalize input
        target = target.lower().strip()
        if target not in ["edge1", "edge2"]:
            return f"❌ Invalid target: '{target}'. Must be 'edge1' or 'edge2'"
        
        base_url = f"{CONFIG.nef_url}/3gpp-traffic-influence/v1/{CONFIG.af_id}/subscriptions"
        
        try:
            # Step 1: Delete any existing subscriptions
            resp = requests.get(base_url, timeout=10)
            if resp.status_code == 200:
                subs = resp.json() if resp.text and resp.text != "null" else []
                if subs:
                    for sub in subs:
                        sub_id = sub.get("self", "").split("/")[-1]
                        if sub_id:
                            requests.delete(f"{base_url}/{sub_id}", timeout=10)
                    time.sleep(1)
            
            # Step 2: Create new subscription
            payload = {
                "afServiceId": "steering",
                "afAppId": "traffic-steering-agent",
                "dnn": CONFIG.dnn,
                "snssai": {
                    "sst": CONFIG.sst,
                    "sd": CONFIG.sd
                },
                "anyUeInd": True,
                "trafficFilters": [{
                    "flowId": 1,
                    "flowDescriptions": ["permit out ip from any to any"]
                }],
                "trafficRoutes": [{
                    "dnai": target
                }]
            }
            
            resp = requests.post(base_url, json=payload, timeout=10)
            
            if resp.status_code in [200, 201]:
                data = resp.json() if resp.text else {}
                sub_id = data.get("self", "").split("/")[-1]
                
                expected_pool = "10.1.0.0/17" if target == "edge1" else "10.1.128.0/17"
                upf_name = "AnchorUPF1" if target == "edge1" else "AnchorUPF2"
                
                return f"""✅ Traffic steering subscription created!

Target DNAI: {target}
Target UPF: {upf_name}
Expected IP Pool: {expected_pool}
Subscription ID: {sub_id}

Traffic will be routed through {upf_name}."""
            else:
                return f"❌ Failed to create subscription: HTTP {resp.status_code} - {resp.text}"
                
        except requests.exceptions.ConnectionError:
            return f"❌ Cannot connect to NEF at {CONFIG.nef_url}"
        except Exception as e:
            return f"❌ Error: {str(e)}"


# ============================================================================
# Main Agent Class
# ============================================================================

class TrafficSteeringAgent:
    """Simple LLM-powered Traffic Steering Agent"""
    
    def __init__(self):
        """Initialize the agent"""
        print("🤖 Initializing Traffic Steering Agent...")
        
        # Set Ollama API base
        os.environ["OLLAMA_API_BASE"] = CONFIG.ollama_base
        
        # Initialize the model
        self.model = LiteLLMModel(
            model_id=f"ollama/{CONFIG.model_name}",
            api_base=CONFIG.ollama_base
        )
        
        # Initialize just 2 tools
        self.tools = [
            GetUPFNetworkMetricsTool(),
            SteerTrafficTool(),
        ]
        
        # Create the agent
        self.agent = CodeAgent(
            tools=self.tools,
            model=self.model,
            verbosity_level=1
        )
        
        print("✅ Agent ready with 2 tools: get_upf_network_metrics, steer_traffic")
    
    def process(self, request: str) -> str:
        """Process a user request"""
        try:
            return str(self.agent.run(request))
        except Exception as e:
            return f"❌ Error: {str(e)}"


# ============================================================================
# Auto-Steering Monitor
# ============================================================================

class AutoSteeringMonitor:
    """
    Background monitor that uses LLM to make intelligent traffic steering decisions.
    
    Logic:
    - Periodically queries UPF traffic metrics
    - Asks the LLM agent to analyze the situation and decide if steering is needed
    - LLM has access to metrics and steering tools to make autonomous decisions
    - Has cooldown to prevent flapping
    """
    
    def __init__(self, agent):
        self.agent = agent  # LLM agent that will make steering decisions
        self.current_target = None  # Start with no active policy
        self.last_steer_time = 0
        self.running = False
        self.thread = None
        
        # Set threshold gauge
        AUTO_STEER_THRESHOLD.set(CONFIG.auto_steer_threshold_bps)
        CURRENT_TARGET.set(0)  # Start with no policy (0=none, 1=edge1, 2=edge2)
        
        print(f"📊 Auto-steering config (LLM-driven):")
        print(f"   Enabled: {CONFIG.auto_steer_enabled}")
        print(f"   Interval: {CONFIG.auto_steer_interval}s")
        print(f"   Threshold: {CONFIG.auto_steer_threshold_bps/1000:.1f} KB/s")
        print(f"   Cooldown: {CONFIG.auto_steer_cooldown}s")
    
    def get_active_policy(self) -> str | None:
        """
        Check NEF for any active traffic influence subscriptions.
        Returns 'edge1', 'edge2', or None if no active policy.
        """
        base_url = f"{CONFIG.nef_url}/3gpp-traffic-influence/v1/{CONFIG.af_id}/subscriptions"
        
        try:
            resp = requests.get(base_url, timeout=10)
            if resp.status_code == 200:
                subs = resp.json() if resp.text and resp.text != "null" else []
                if subs and len(subs) > 0:
                    # Check the first subscription's target DNAI
                    for sub in subs:
                        routes = sub.get("trafficRoutes", [])
                        for route in routes:
                            dnai = route.get("dnai", "").lower()
                            if dnai in ["edge1", "edge2"]:
                                return dnai
            return None
        except Exception as e:
            print(f"⚠️  Error checking active policy: {e}")
            return None
    
    def get_upf_traffic_rates(self) -> dict:
        """Query Prometheus for UPF traffic rates (bytes/sec)"""
        rates = {"edge1": 0.0, "edge2": 0.0, "upfb": 0.0}
        
        try:
            # Query rate of traffic on UPF anchor pods
            # UPF1 = edge1, UPF2 = edge2, UPFB = branching UPF
            query = 'sum(rate(container_network_receive_bytes_total{namespace="free5gc",pod=~".*upf.*"}[1m])) by (pod)'
            
            resp = requests.get(
                f"{CONFIG.prometheus_url}/api/v1/query",
                params={"query": query},
                timeout=10
            )
            
            if resp.status_code == 200:
                data = resp.json()
                for result in data.get("data", {}).get("result", []):
                    pod = result["metric"].get("pod", "")
                    rate = float(result["value"][1])
                    
                    # Map pod names to edge targets
                    if "upf1" in pod.lower() or "anchor" in pod.lower() and "1" in pod:
                        rates["edge1"] += rate
                    elif "upf2" in pod.lower() or "anchor" in pod.lower() and "2" in pod:
                        rates["edge2"] += rate
                    elif "upfb" in pod.lower():
                        # Branching UPF - track N3/N6 traffic
                        rates["upfb"] += rate
                        
                # Update Prometheus gauges
                UPF_TRAFFIC_RATE.labels(upf="edge1").set(rates["edge1"])
                UPF_TRAFFIC_RATE.labels(upf="edge2").set(rates["edge2"])
                UPF_TRAFFIC_RATE.labels(upf="upfb").set(rates["upfb"])
                
        except Exception as e:
            print(f"⚠️  Error querying traffic rates: {e}")
        
        return rates
    
    def format_metrics_summary(self, rates: dict) -> str:
        """Format current metrics into a human-readable summary for the LLM"""
        edge1_kb = rates["edge1"] / 1000
        edge2_kb = rates["edge2"] / 1000
        upfb_kb = rates["upfb"] / 1000
        threshold_kb = CONFIG.auto_steer_threshold_bps / 1000
        
        summary = f"""Current UPF Traffic Status:
- Edge1 (AnchorUPF1): {edge1_kb:.2f} KB/s
- Edge2 (AnchorUPF2): {edge2_kb:.2f} KB/s
- UPFB (Branching UPF): {upfb_kb:.2f} KB/s
- Current active policy: {self.current_target or 'none'}
- Steering threshold: {threshold_kb:.2f} KB/s
- Time since last steer: {time.time() - self.last_steer_time:.0f} seconds
- Cooldown period: {CONFIG.auto_steer_cooldown} seconds"""
        
        return summary
    
    def ask_llm_for_decision(self, rates: dict) -> dict:
        """
        Ask the LLM agent to analyze metrics and decide if steering is needed.
        
        Returns dict with 'should_steer' (bool) and 'target' (str or None)
        """
        # Check cooldown first
        time_since_last_steer = time.time() - self.last_steer_time
        if time_since_last_steer < CONFIG.auto_steer_cooldown:
            return {"should_steer": False, "target": None, "reason": "cooldown"}
        
        # Update current policy state
        active_policy = self.get_active_policy()
        if active_policy != self.current_target:
            print(f"📋 Policy state updated: {self.current_target} → {active_policy or 'none'}")
            self.current_target = active_policy
            if active_policy == "edge1":
                CURRENT_TARGET.set(1)
            elif active_policy == "edge2":
                CURRENT_TARGET.set(2)
            else:
                CURRENT_TARGET.set(0)
        
        # Prepare simple metrics for LLM
        edge1_kb = rates["edge1"] / 1000
        edge2_kb = rates["edge2"] / 1000
        upfb_kb = rates["upfb"] / 1000
        threshold_kb = CONFIG.auto_steer_threshold_bps / 1000
        
        # Construct simplified prompt for LLM
        prompt = f"""You are a 5G traffic steering expert. Based on these metrics, decide if traffic steering is needed.

METRICS:
- Edge1 traffic: {edge1_kb:.2f} KB/s
- Edge2 traffic: {edge2_kb:.2f} KB/s
- UPFB traffic: {upfb_kb:.2f} KB/s
- Active policy: {active_policy or 'none'}
- Threshold: {threshold_kb:.2f} KB/s

RULES:
1. If no policy exists and UPFB > threshold: steer to the edge with LOWER traffic (if equal, choose edge1)
2. If policy exists and that edge > threshold: rebalance to the other edge (if it has 20% less traffic)
3. Otherwise: no action needed

Respond with EXACTLY one word:
- edge1 (to steer to edge1)
- edge2 (to steer to edge2)
- none (no steering needed)

IMPORTANT: If UPFB traffic exceeds threshold and no policy exists, you MUST choose edge1 or edge2, NOT none!

Your decision:"""
        
        try:
            # Ask LLM for decision
            print(f"🤖 Asking LLM for steering decision...")
            response = self.agent.process(prompt)
            response_clean = response.strip().lower()
            
            # Extract just the decision word
            if "edge1" in response_clean:
                print(f"🤖 LLM decision: STEER edge1")
                return {"should_steer": True, "target": "edge1", "reason": "llm_decision"}
            elif "edge2" in response_clean:
                print(f"🤖 LLM decision: STEER edge2")
                return {"should_steer": True, "target": "edge2", "reason": "llm_decision"}
            else:
                print(f"🤖 LLM decision: NO_STEER (response: {response_clean[:100]})")
                return {"should_steer": False, "target": None, "reason": "llm_no_action"}
                
        except Exception as e:
            print(f"❌ LLM decision error: {e}")
            return {"should_steer": False, "target": None, "reason": "error"}
    
    def execute_steering(self, target: str, reason: str = "llm_decision"):
        """Execute steering action using the agent's tools"""
        old_target = self.current_target
        
        print(f"🚀 LLM-driven auto-steering: {old_target} → {target}")
        
        # Use the agent to execute steering
        try:
            steer_prompt = f"Steer traffic to {target}"
            result = self.agent.process(steer_prompt)
            
            if "✅" in result or "success" in result.lower():
                self.current_target = target
                self.last_steer_time = time.time()
                CURRENT_TARGET.set(1 if target == "edge1" else 2)
                AUTO_STEER_TRIGGERS.labels(
                    from_target=old_target or "none",
                    to_target=target,
                    reason=reason
                ).inc()
                print(f"✅ LLM auto-steer successful: now routing through {target}")
            else:
                print(f"❌ LLM auto-steer failed: {result}")
        except Exception as e:
            print(f"❌ Error executing steering: {e}")
    
    def monitor_loop(self):
        """Main monitoring loop - uses LLM to make steering decisions"""
        print(f"🔍 LLM-driven auto-steering monitor started (interval: {CONFIG.auto_steer_interval}s)")
        
        while self.running:
            try:
                # Get current traffic rates
                rates = self.get_upf_traffic_rates()
                
                # Log current status periodically (include UPFB and active policy)
                policy_str = self.current_target if self.current_target else "none"
                print(f"📈 Traffic rates - edge1: {rates['edge1']/1000:.1f} KB/s, edge2: {rates['edge2']/1000:.1f} KB/s, upfb: {rates['upfb']/1000:.1f} KB/s (policy: {policy_str})")
                
                # Ask LLM for steering decision
                decision = self.ask_llm_for_decision(rates)
                
                if decision["should_steer"] and decision["target"]:
                    self.execute_steering(decision["target"], decision["reason"])
                else:
                    print(f"🤖 LLM decision: No steering needed ({decision['reason']})")
                
            except Exception as e:
                print(f"⚠️  Monitor error: {e}")
            
            # Wait for next interval
            time.sleep(CONFIG.auto_steer_interval)
    
    def start(self):
        """Start the monitoring thread"""
        if not CONFIG.auto_steer_enabled:
            print("⏸️  Auto-steering is disabled")
            return
        
        if self.running:
            return
        
        self.running = True
        self.thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.thread.start()
    
    def stop(self):
        """Stop the monitoring thread"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)


# Global monitor instance
auto_monitor = None


# ============================================================================
# Flask HTTP Server
# ============================================================================

from flask import Flask, request as flask_request, jsonify
import logging

# Suppress health check logs
class HealthCheckFilter(logging.Filter):
    def filter(self, record):
        return '/health' not in record.getMessage()

app = Flask(__name__)

# Apply filter to werkzeug logger to suppress health check logs
logging.getLogger('werkzeug').addFilter(HealthCheckFilter())

agent = None


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    start = time.time()
    REQUEST_TOTAL.labels(endpoint='/health', method='GET', status='200').inc()
    REQUEST_LATENCY.labels(endpoint='/health', method='GET').observe(time.time() - start)
    return jsonify({"status": "healthy"}), 200


@app.route('/metrics', methods=['GET'])
def metrics():
    """Direct endpoint to get UPF metrics (no LLM)"""
    start = time.time()
    ACTIVE_REQUESTS.labels(endpoint='/metrics').inc()
    try:
        tool = GetUPFNetworkMetricsTool()
        result = tool.forward()
        REQUEST_TOTAL.labels(endpoint='/metrics', method='GET', status='200').inc()
        REQUEST_LATENCY.labels(endpoint='/metrics', method='GET').observe(time.time() - start)
        return result, 200, {'Content-Type': 'text/plain'}
    except Exception as e:
        REQUEST_TOTAL.labels(endpoint='/metrics', method='GET', status='500').inc()
        REQUEST_LATENCY.labels(endpoint='/metrics', method='GET').observe(time.time() - start)
        return f"Error: {e}", 500
    finally:
        ACTIVE_REQUESTS.labels(endpoint='/metrics').dec()


@app.route('/steer/<target>', methods=['POST'])
def steer(target):
    """Direct endpoint to steer traffic (no LLM)"""
    start = time.time()
    ACTIVE_REQUESTS.labels(endpoint='/steer').inc()
    try:
        tool = SteerTrafficTool()
        result = tool.forward(target)
        
        if result.startswith("✅"):
            status = "success"
            http_status = "200"
            # Set current target gauge (1=edge1, 2=edge2)
            CURRENT_TARGET.set(1 if target == "edge1" else 2)
        else:
            status = "failed"
            http_status = "400"
        
        STEERING_OPERATIONS.labels(target=target, status=status).inc()
        REQUEST_TOTAL.labels(endpoint=f'/steer/{target}', method='POST', status=http_status).inc()
        REQUEST_LATENCY.labels(endpoint=f'/steer/{target}', method='POST').observe(time.time() - start)
        
        return result, 200, {'Content-Type': 'text/plain'}
    except Exception as e:
        STEERING_OPERATIONS.labels(target=target, status='error').inc()
        REQUEST_TOTAL.labels(endpoint=f'/steer/{target}', method='POST', status='500').inc()
        REQUEST_LATENCY.labels(endpoint=f'/steer/{target}', method='POST').observe(time.time() - start)
        return f"Error: {e}", 500
    finally:
        ACTIVE_REQUESTS.labels(endpoint='/steer').dec()


@app.route('/chat', methods=['POST'])
def chat():
    """LLM-powered chat endpoint"""
    global agent
    start = time.time()
    ACTIVE_REQUESTS.labels(endpoint='/chat').inc()
    
    try:
        data = flask_request.get_json()
        if not data or 'message' not in data:
            REQUEST_TOTAL.labels(endpoint='/chat', method='POST', status='400').inc()
            return jsonify({"error": "Missing 'message' field"}), 400
        
        response = agent.process(data['message'])
        REQUEST_TOTAL.labels(endpoint='/chat', method='POST', status='200').inc()
        REQUEST_LATENCY.labels(endpoint='/chat', method='POST').observe(time.time() - start)
        return jsonify({"response": response}), 200
    except Exception as e:
        REQUEST_TOTAL.labels(endpoint='/chat', method='POST', status='500').inc()
        REQUEST_LATENCY.labels(endpoint='/chat', method='POST').observe(time.time() - start)
        return jsonify({"error": str(e)}), 500
    finally:
        ACTIVE_REQUESTS.labels(endpoint='/chat').dec()


@app.route('/agent-metrics', methods=['GET'])
def agent_metrics():
    """Prometheus metrics endpoint for SLO monitoring"""
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}


@app.route('/auto-steer/status', methods=['GET'])
def auto_steer_status():
    """Get auto-steering status"""
    global auto_monitor
    if auto_monitor:
        return jsonify({
            "enabled": CONFIG.auto_steer_enabled,
            "running": auto_monitor.running,
            "current_target": auto_monitor.current_target,
            "threshold_bps": CONFIG.auto_steer_threshold_bps,
            "interval_seconds": CONFIG.auto_steer_interval,
            "cooldown_seconds": CONFIG.auto_steer_cooldown,
            "last_steer_time": auto_monitor.last_steer_time
        }), 200
    return jsonify({"error": "Auto-monitor not initialized"}), 500


@app.route('/auto-steer/enable', methods=['POST'])
def auto_steer_enable():
    """Enable auto-steering"""
    global auto_monitor
    if auto_monitor and not auto_monitor.running:
        auto_monitor.running = True
        auto_monitor.thread = threading.Thread(target=auto_monitor.monitor_loop, daemon=True)
        auto_monitor.thread.start()
        return jsonify({"status": "Auto-steering enabled"}), 200
    return jsonify({"status": "Already running or not initialized"}), 200


@app.route('/auto-steer/disable', methods=['POST'])
def auto_steer_disable():
    """Disable auto-steering"""
    global auto_monitor
    if auto_monitor:
        auto_monitor.running = False
        return jsonify({"status": "Auto-steering disabled"}), 200
    return jsonify({"error": "Auto-monitor not initialized"}), 500


@app.route('/auto-steer/threshold', methods=['POST'])
def auto_steer_threshold():
    """Update auto-steering threshold"""
    global auto_monitor
    data = flask_request.get_json()
    if data and 'threshold_bps' in data:
        CONFIG.auto_steer_threshold_bps = float(data['threshold_bps'])
        AUTO_STEER_THRESHOLD.set(CONFIG.auto_steer_threshold_bps)
        return jsonify({
            "status": "Threshold updated",
            "threshold_bps": CONFIG.auto_steer_threshold_bps
        }), 200
    return jsonify({"error": "Missing threshold_bps"}), 400


# ============================================================================
# Main
# ============================================================================

def main():
    global agent, auto_monitor
    
    print("=" * 60)
    print("🌐 Traffic Steering Agent (with LLM-Driven Auto-Steering)")
    print("=" * 60)
    
    agent = TrafficSteeringAgent()
    
    # Initialize auto-steering monitor with the LLM agent
    auto_monitor = AutoSteeringMonitor(agent)
    
    # Check if running in K8s
    in_k8s = os.path.exists('/var/run/secrets/kubernetes.io') or os.getenv('KUBERNETES_SERVICE_HOST')
    
    if in_k8s:
        # Start auto-steering monitor
        auto_monitor.start()
        
        print("\n📡 HTTP Endpoints:")
        print("  GET  /health             - Health check")
        print("  GET  /metrics            - Get UPF network metrics (direct)")
        print("  POST /steer/edge1        - Steer to edge1 (direct)")
        print("  POST /steer/edge2        - Steer to edge2 (direct)")
        print("  POST /chat               - LLM chat (JSON: {'message': '...'})")
        print("  GET  /agent-metrics      - Prometheus metrics (SLO)")
        print("  GET  /auto-steer/status  - Auto-steering status")
        print("  POST /auto-steer/enable  - Enable auto-steering")
        print("  POST /auto-steer/disable - Disable auto-steering")
        print("  POST /auto-steer/threshold - Set threshold (JSON: {'threshold_bps': N})")
        print("=" * 60)
        app.run(host='0.0.0.0', port=8080)
    else:
        # Interactive mode (no auto-steering)
        print("\n📋 Available commands:")
        print("  - Ask about UPF network usage")
        print("  - Steer traffic to edge1 or edge2")
        print("=" * 60)
        
        while True:
            try:
                user_input = input("\n💬 You: ").strip()
                if user_input.lower() in ['quit', 'exit', 'q']:
                    break
                if user_input:
                    response = agent.process(user_input)
                    print(f"\n🤖 Agent:\n{response}")
            except KeyboardInterrupt:
                break
        print("👋 Goodbye!")


if __name__ == "__main__":
    main()
