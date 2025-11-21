import time
import subprocess
import re
import requests
from smolagents import CodeAgent, tool, LiteLLMModel
from prometheus_client import start_http_server, Gauge, Counter, Histogram

# --- PROMETHEUS METRICS DEFINITION ---
# 1. Input: What the agent sees
NETWORK_LATENCY = Gauge('network_observed_latency_ms', 'Real-time network latency measured by the agent')

# 2. Output: What the agent decided (0=Core, 1=MEC for easy graphing)
CURRENT_DECISION = Gauge('agent_decision_state', 'Current traffic path: 0=Core, 1=MEC')

# 3. Performance: How long the AI takes to think
AGENT_THINK_TIME = Histogram('agent_processing_seconds', 'Time taken for the LLM to generate a plan')

# 4. Quality: Did we violate the SLA?
SLA_VIOLATIONS = Counter('sla_violation_total', 'Total count of SLA breaches (>50ms)')

# 5. Reliability: Did the steering request fail?
STEERING_ERRORS = Counter('nef_steering_errors_total', 'Total failed requests to NEF')

# --- CONFIGURATION ---
NEF_URL = "http://10.152.183.217:80/3gpp-traffic-influence/v1/af001/subscriptions"
PING_TARGET = "8.8.8.8" 
SLA_THRESHOLD_MS = 50.0

# --- TOOLS ---
@tool
def get_real_latency() -> str:
    """
    Runs a system ping to measure network latency. 
    """
    try:
        command = ["ping", "-c", "3", "-W", "1", PING_TARGET]
        result = subprocess.run(command, stdout=subprocess.PIPE, text=True)
        
        # Extract Latency
        match = re.search(r"min/avg/max/.* = [\d\.]+/([\d\.]+)/", result.stdout)
        if match:
            latency = float(match.group(1))
            
            # [METRIC] Record the raw value
            NETWORK_LATENCY.set(latency)
            
            # [METRIC] Check SLA
            if latency > SLA_THRESHOLD_MS:
                SLA_VIOLATIONS.inc()
                
            return f"Current Latency: {latency}ms"
        return "ERROR: Could not parse ping."
    except Exception as e:
        return f"ERROR: {str(e)}"

@tool
def apply_traffic_steering(destination_dnai: str) -> str:
    """
    Sends a request to 5G NEF. Destination must be 'mec' or 'core'.
    """
    # [METRIC] Update the state gauge for visualization
    if destination_dnai.lower() == 'mec':
        CURRENT_DECISION.set(1)
    else:
        CURRENT_DECISION.set(0)

    # Payload (Standard 3GPP)
    payload = {
        "afServiceId": "Service1",
        "dnn": "internet",
        "snssai": {"sst": 1, "sd": "010203"},
        "anyUeInd": True,
        "trafficRoutes": [{"dnai": destination_dnai}]
    }
    
    try:
        # requests.post(NEF_URL, json=payload) # Uncomment for real action
        return f"SUCCESS: Traffic routed to {destination_dnai}"
    except Exception as e:
        # [METRIC] Record API failures
        STEERING_ERRORS.inc()
        return f"ERROR: {str(e)}"

# --- AGENT SETUP ---
model = LiteLLMModel(model_id="ollama/llama3", api_base="http://localhost:11434")
agent = CodeAgent(tools=[get_real_latency, apply_traffic_steering], model=model, add_base_tools=False)

def start_autonomous_sre():
    # Start Prometheus Server on Port 8000
    start_http_server(8000)
    print("✅ Prometheus Metrics running at http://localhost:8000/metrics")

    instruction = """
    1. Check latency.
    2. If > 50ms, steer to 'mec'.
    3. If <= 50ms, steer to 'core'.
    """

    while True:
        print("\n--- New Analysis Cycle ---")
        
        # [METRIC] Time the AI's thinking process
        with AGENT_THINK_TIME.time():
            try:
                agent.run(instruction)
            except Exception as e:
                print(f"Agent Hallucination/Error: {e}")

        time.sleep(5)

if __name__ == "__main__":
    start_autonomous_sre()