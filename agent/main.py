import time
import requests
import json
import ollama
from prometheus_client import start_http_server
import constants

def get_network_load():
    """Queries Prometheus for real-time bandwidth."""
    try:
        response = requests.get(
            f"{constants.PROMETHEUS_URL}/api/v1/query",
            params={'query': constants.METRIC_QUERY},
            timeout=2
        )
        result = response.json()['data']['result']
        if result:
            mbps = float(result[0]['value'][1])
            return round(mbps, 2)
        return 0.0
    except Exception as e:
        print(f"Error reading Prometheus: {e}")
        constants.AGENT_ERRORS.labels(type='prometheus_read').inc()
        return 0.0

@constants.DECISION_LATENCY.time() # Measures execution time of this function
def ask_ai_decision(current_load, current_route):
    """Sends network status to Ollama and asks for a JSON policy decision."""
    
    system_prompt = f"""
    You are a 5G Network Optimization Engine.
    
    Network Topology:
    - Central Route: {constants.CENTRAL_UPF_IP}
    - Edge Route:    {constants.EDGE_UPF_IP}
    
    Policy Rules:
    1. If Load < 10 Mbps: Use Central Route.
    2. If Load >= 10 Mbps: Use Edge Route.
    
    Output MUST be valid JSON only. Structure:
    {{
      "trafficRoutes": [{{
          "routeInfo": {{
              "ipv4Addr": "IP_ADDRESS"
          }}
      }}]
    }}
    """

    user_prompt = f"Current Load: {current_load} Mbps. Current Route: {current_route}. JSON Decision:"

    try:
        response = ollama.chat(model=constants.OLLAMA_MODEL, messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ])
        
        content = response['message']['content']
        # Cleanup code blocks if present
        if "```" in content:
            content = content.split("```json")[-1].split("```")[0].strip()
            
        return json.loads(content)
    except Exception as e:
        print(f"AI Error or Invalid JSON: {e}")
        constants.AGENT_ERRORS.labels(type='ai_parse').inc()
        return None

def apply_traffic_steering(policy_json):
    """Pushes the AI's decision to the 5G Core via NEF."""
    url = f"{constants.NEF_URL}/3gpp-traffic-influence/v1/{constants.AF_ID}/subscriptions/{constants.SUBSCRIPTION_ID}"
    headers = {'Content-Type': 'application/json'}
    
    # Ensure required 3GPP fields exist
    policy_json['afAppId'] = "SmartAgent"
    policy_json['dnn'] = "internet"
    
    try:
        resp = requests.put(url, json=policy_json, headers=headers, timeout=5)
        
        if resp.status_code in [200, 201]:
            target_ip = policy_json['trafficRoutes'][0]['routeInfo']['ipv4Addr']
            print(f"SUCCESS: Traffic steered to {target_ip}")
            constants.STEERING_EVENTS.labels(target_route=target_ip).inc()
            return target_ip
        else:
            print(f"NEF Error {resp.status_code}: {resp.text}")
            constants.AGENT_ERRORS.labels(type='nef_api').inc()
            return None
    except Exception as e:
        print(f"Connection Failed: {e}")
        constants.AGENT_ERRORS.labels(type='nef_api').inc()
        return None

# --- MAIN LOOP ---
if __name__ == "__main__":
    # Start Prometheus Metrics Server
    start_http_server(8000)
    print(f"Agent started. Metrics exposed on :8000. Using model: {constants.OLLAMA_MODEL}")
    
    current_route_ip = constants.CENTRAL_UPF_IP
    
    while True:
        load = get_network_load()
        print(f"Current Load: {load} Mbps | Route: {current_route_ip}")
        
        policy = ask_ai_decision(load, current_route_ip)
        
        if policy:
            try:
                proposed_ip = policy['trafficRoutes'][0]['routeInfo']['ipv4Addr']
                
                if proposed_ip != current_route_ip:
                    print(f"Policy Mismatch Detected. Applying Fix...")
                    result_ip = apply_traffic_steering(policy)
                    if result_ip:
                        current_route_ip = result_ip
                else:
                    print("No route change needed.")
            except KeyError:
                print("Invalid JSON structure from AI")
                constants.AGENT_ERRORS.labels(type='ai_parse').inc()
        
        time.sleep(constants.POLL_INTERVAL)