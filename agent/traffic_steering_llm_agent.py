#!/usr/bin/env python3
"""
Traffic Steering LLM Agent (Simplified)
=======================================
A simple AI-powered agent for 5G ULCL traffic steering with two core functions:
1. Query UPF network metrics from Prometheus
2. Steer traffic to edge1 or edge2 via NEF API
"""

import os
import json
import time
from dataclasses import dataclass

import requests
from smolagents import Tool, CodeAgent, LiteLLMModel


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
# Flask HTTP Server
# ============================================================================

from flask import Flask, request as flask_request, jsonify

app = Flask(__name__)
agent = None


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy"}), 200


@app.route('/metrics', methods=['GET'])
def metrics():
    """Direct endpoint to get UPF metrics (no LLM)"""
    tool = GetUPFNetworkMetricsTool()
    result = tool.forward()
    return result, 200, {'Content-Type': 'text/plain'}


@app.route('/steer/<target>', methods=['POST'])
def steer(target):
    """Direct endpoint to steer traffic (no LLM)"""
    tool = SteerTrafficTool()
    result = tool.forward(target)
    return result, 200, {'Content-Type': 'text/plain'}


@app.route('/chat', methods=['POST'])
def chat():
    """LLM-powered chat endpoint"""
    global agent
    data = flask_request.get_json()
    if not data or 'message' not in data:
        return jsonify({"error": "Missing 'message' field"}), 400
    
    response = agent.process(data['message'])
    return jsonify({"response": response}), 200


# ============================================================================
# Main
# ============================================================================

def main():
    global agent
    
    print("=" * 60)
    print("🌐 Traffic Steering Agent (Simplified)")
    print("=" * 60)
    
    agent = TrafficSteeringAgent()
    
    # Check if running in K8s
    in_k8s = os.path.exists('/var/run/secrets/kubernetes.io') or os.getenv('KUBERNETES_SERVICE_HOST')
    
    if in_k8s:
        print("\n📡 HTTP Endpoints:")
        print("  GET  /health       - Health check")
        print("  GET  /metrics      - Get UPF network metrics (direct)")
        print("  POST /steer/edge1  - Steer to edge1 (direct)")
        print("  POST /steer/edge2  - Steer to edge2 (direct)")
        print("  POST /chat         - LLM chat (JSON: {'message': '...'})")
        print("=" * 60)
        app.run(host='0.0.0.0', port=8080)
    else:
        # Interactive mode
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
