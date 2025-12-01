#!/usr/bin/env python3
"""
Traffic Steering LLM Agent (Kubernetes Version)
================================================
An AI-powered agent that uses smolagents framework to dynamically manage
5G ULCL traffic steering. Designed to run inside the Kubernetes cluster.

The agent can:
- Monitor UPF traffic metrics from Prometheus
- Steer traffic between edge1 (AnchorUPF1) and edge2 (AnchorUPF2)
- Manage NEF Traffic Influence subscriptions
- Restart UE, SMF, or UPF pods as needed
- Run health checks and diagnostics
"""

import os
import json
import time
import subprocess
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

import requests
from smolagents import Tool, CodeAgent, LiteLLMModel

# Try to import kubernetes client (for in-cluster operations)
try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False
    print("⚠️ kubernetes client not available, using kubectl subprocess")

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class AgentConfig:
    """Configuration for the traffic steering agent"""
    # Kubernetes
    namespace: str = os.getenv("K8S_NAMESPACE", "free5gc")
    kubectl_cmd: str = os.getenv("KUBECTL_CMD", "kubectl")
    in_cluster: bool = os.getenv("IN_CLUSTER", "true").lower() == "true"
    
    # Network Function URLs
    nef_url: str = os.getenv("NEF_URL", "http://free5gc-v1-free5gc-nef-nef-sbi:80")
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://prometheus-kube-prometheus-prometheus.monitoring:9090")
    
    # UERANSIM VM (external to cluster)
    ueransim_host: str = os.getenv("UERANSIM_HOST", "192.168.56.118")
    ueransim_user: str = os.getenv("UERANSIM_USER", "vagrant")
    ueransim_key: str = os.getenv("UERANSIM_KEY", "/app/ssh/ssh-private-key")
    ueransim_path: str = os.getenv("UERANSIM_PATH", "~/ue/UERANSIM")
    
    # Vagrant (for local development)
    use_vagrant: bool = os.getenv("USE_VAGRANT", "false").lower() == "true"
    vagrant_dir: str = os.getenv("VAGRANT_DIR", "/home/elliot/Documents/traffic-steering/vagrant")
    
    # Traffic Steering
    af_id: str = os.getenv("AF_ID", "traffic-steering-agent")
    dnn: str = os.getenv("DNN", "internet")
    sst: int = int(os.getenv("SST", "1"))
    sd: str = os.getenv("SD", "010203")
    
    # LLM
    ollama_base: str = os.getenv("OLLAMA_API_BASE", "http://ollama.ollama:11434")
    model_name: str = os.getenv("LLM_MODEL", "qwen2.5-coder")
    
    # Server
    server_port: int = int(os.getenv("SERVER_PORT", "8080"))


CONFIG = AgentConfig()


# ============================================================================
# Kubernetes Client
# ============================================================================

class K8sClient:
    """Kubernetes client wrapper"""
    
    def __init__(self, config: AgentConfig):
        self.config = config
        self.core_v1 = None
        self.apps_v1 = None
        
        if K8S_AVAILABLE and config.in_cluster:
            try:
                # Load in-cluster config
                kubernetes_config = client.Configuration()
                config_module = __import__('kubernetes.config', fromlist=[''])
                config_module.load_incluster_config(client_configuration=kubernetes_config)
                
                api_client = client.ApiClient(kubernetes_config)
                self.core_v1 = client.CoreV1Api(api_client)
                self.apps_v1 = client.AppsV1Api(api_client)
                logger.info("✅ Kubernetes in-cluster config loaded")
            except Exception as e:
                logger.warning(f"Failed to load in-cluster config: {e}, falling back to kubectl")
    
    def get_pods(self, label_selector: str = "") -> list:
        """Get pods in namespace"""
        if self.core_v1:
            try:
                pods = self.core_v1.list_namespaced_pod(
                    namespace=self.config.namespace,
                    label_selector=label_selector
                )
                return [
                    {
                        "name": pod.metadata.name,
                        "status": pod.status.phase,
                        "ready": all(c.ready for c in (pod.status.container_statuses or []) if c),
                        "restarts": sum(c.restart_count for c in (pod.status.container_statuses or []) if c)
                    }
                    for pod in pods.items
                ]
            except ApiException as e:
                logger.error(f"K8s API error: {e}")
                return []
        else:
            # Fallback to kubectl
            return self._kubectl_get_pods(label_selector)
    
    def _kubectl_get_pods(self, label_selector: str = "") -> list:
        """Get pods using kubectl subprocess"""
        cmd = f"{self.config.kubectl_cmd} -n {self.config.namespace} get pods -o json"
        if label_selector:
            cmd += f" -l {label_selector}"
        
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                return [
                    {
                        "name": item["metadata"]["name"],
                        "status": item["status"]["phase"],
                        "ready": all(c.get("ready", False) for c in item["status"].get("containerStatuses", [])),
                        "restarts": sum(c.get("restartCount", 0) for c in item["status"].get("containerStatuses", []))
                    }
                    for item in data.get("items", [])
                ]
        except Exception as e:
            logger.error(f"kubectl error: {e}")
        return []
    
    def delete_pod(self, pod_name: str) -> bool:
        """Delete a pod"""
        if self.core_v1:
            try:
                self.core_v1.delete_namespaced_pod(
                    name=pod_name,
                    namespace=self.config.namespace,
                    grace_period_seconds=0
                )
                return True
            except ApiException as e:
                logger.error(f"Failed to delete pod: {e}")
                return False
        else:
            cmd = f"{self.config.kubectl_cmd} -n {self.config.namespace} delete pod {pod_name} --force --grace-period=0"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            return result.returncode == 0 or "deleted" in result.stdout.lower()
    
    def restart_deployment(self, deployment_name: str) -> bool:
        """Restart a deployment"""
        if self.apps_v1:
            try:
                # Patch the deployment to trigger rollout
                patch = {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "kubectl.kubernetes.io/restartedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ")
                                }
                            }
                        }
                    }
                }
                self.apps_v1.patch_namespaced_deployment(
                    name=deployment_name,
                    namespace=self.config.namespace,
                    body=patch
                )
                return True
            except ApiException as e:
                logger.error(f"Failed to restart deployment: {e}")
                return False
        else:
            cmd = f"{self.config.kubectl_cmd} -n {self.config.namespace} rollout restart deployment/{deployment_name}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            return result.returncode == 0
    
    def get_pod_logs(self, pod_name: str, lines: int = 50) -> str:
        """Get pod logs"""
        if self.core_v1:
            try:
                logs = self.core_v1.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=self.config.namespace,
                    tail_lines=lines
                )
                return logs
            except ApiException as e:
                return f"Error: {e}"
        else:
            cmd = f"{self.config.kubectl_cmd} -n {self.config.namespace} logs {pod_name} --tail={lines}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            return result.stdout if result.returncode == 0 else result.stderr


# Global K8s client
K8S_CLIENT: Optional[K8sClient] = None


def get_k8s_client() -> K8sClient:
    """Get or create K8s client"""
    global K8S_CLIENT
    if K8S_CLIENT is None:
        K8S_CLIENT = K8sClient(CONFIG)
    return K8S_CLIENT


# ============================================================================
# Helper Functions
# ============================================================================

def run_ue_command(command: str, timeout: int = 30) -> tuple[bool, str]:
    """Run command on UERANSIM VM via SSH"""
    if CONFIG.use_vagrant:
        cmd = f"cd {CONFIG.vagrant_dir} && vagrant ssh vm3 -c \"{command}\""
    else:
        ssh_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
        key_opt = f"-i {CONFIG.ueransim_key}" if CONFIG.ueransim_key and os.path.exists(CONFIG.ueransim_key) else ""
        cmd = f"ssh {ssh_opts} {key_opt} {CONFIG.ueransim_user}@{CONFIG.ueransim_host} \"{command}\""
    
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout.strip() or result.stderr.strip()
    except Exception as e:
        return False, str(e)


# ============================================================================
# Tools for the LLM Agent
# ============================================================================

class GetUPFMetricsTool(Tool):
    """Tool to get traffic metrics from Prometheus for UPFs"""
    
    name = "get_upf_metrics"
    description = """
    Get real-time traffic metrics for the UPFs (User Plane Functions) from Prometheus.
    Returns TX/RX rates in Mbps for AnchorUPF1 (edge1) and AnchorUPF2 (edge2).
    Use this to understand current traffic load before making steering decisions.
    """
    inputs = {
        "upf": {
            "type": "string",
            "description": "Which UPF to get metrics for: 'upf1' (edge1), 'upf2' (edge2), or 'all' for both"
        }
    }
    output_type = "string"
    
    def forward(self, upf: str = "all") -> str:
        """Query Prometheus for UPF metrics"""
        results = {}
        upfs_to_query = ["upf1", "upf2"] if upf == "all" else [upf]
        
        for upf_name in upfs_to_query:
            try:
                tx_query = f'rate(container_network_transmit_bytes_total{{namespace="free5gc",pod=~".*{upf_name}.*",interface="n6"}}[1m]) * 8 / 1000000'
                resp = requests.get(f"{CONFIG.prometheus_url}/api/v1/query", params={"query": tx_query}, timeout=10)
                
                tx_rate = 0.0
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("data", {}).get("result"):
                        tx_rate = float(data["data"]["result"][0]["value"][1])
                
                rx_query = f'rate(container_network_receive_bytes_total{{namespace="free5gc",pod=~".*{upf_name}.*",interface="n6"}}[1m]) * 8 / 1000000'
                resp = requests.get(f"{CONFIG.prometheus_url}/api/v1/query", params={"query": rx_query}, timeout=10)
                
                rx_rate = 0.0
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("data", {}).get("result"):
                        rx_rate = float(data["data"]["result"][0]["value"][1])
                
                edge = "edge1" if upf_name == "upf1" else "edge2"
                results[upf_name] = {
                    "edge": edge,
                    "tx_rate_mbps": round(tx_rate, 3),
                    "rx_rate_mbps": round(rx_rate, 3),
                    "total_rate_mbps": round(tx_rate + rx_rate, 3)
                }
            except Exception as e:
                results[upf_name] = {"error": str(e)}
        
        return json.dumps(results, indent=2)


class ListNEFSubscriptionsTool(Tool):
    """Tool to list current NEF Traffic Influence subscriptions"""
    
    name = "list_nef_subscriptions"
    description = """
    List all active Traffic Influence subscriptions in the NEF (Network Exposure Function).
    Shows which DNAI (Data Network Access Identifier) is currently active for traffic steering.
    """
    inputs = {}
    output_type = "string"
    
    def forward(self) -> str:
        """List subscriptions from NEF"""
        try:
            url = f"{CONFIG.nef_url}/3gpp-traffic-influence/v1/{CONFIG.af_id}/subscriptions"
            resp = requests.get(url, timeout=10)
            
            if resp.status_code == 200:
                subs = resp.json() if resp.text else []
                
                if not subs:
                    return "No active subscriptions found. Traffic is following default routing."
                
                result = []
                for sub in subs:
                    sub_id = sub.get("self", "").split("/")[-1]
                    dnai = sub.get("trafficRoutes", [{}])[0].get("dnai", "N/A")
                    result.append({
                        "subscription_id": sub_id,
                        "dnai": dnai,
                        "dnn": sub.get("dnn", "N/A"),
                        "any_ue": sub.get("anyUeInd", False)
                    })
                
                return json.dumps(result, indent=2)
            else:
                return f"❌ Failed to list subscriptions: HTTP {resp.status_code}"
                
        except requests.exceptions.ConnectionError:
            return "❌ Cannot connect to NEF. Is it running?"
        except Exception as e:
            return f"❌ Error: {str(e)}"


class SteerTrafficTool(Tool):
    """Tool to steer traffic to a specific DNAI (edge1 or edge2)"""
    
    name = "steer_traffic"
    description = """
    Steer UE traffic to a specific edge UPF by creating a NEF Traffic Influence subscription.
    
    - 'edge1' steers to AnchorUPF1 (IP pool: 10.1.0.0/17)
    - 'edge2' steers to AnchorUPF2 (IP pool: 10.1.128.0/17)
    
    This will:
    1. Delete any existing subscriptions
    2. Create a new subscription for the target DNAI
    
    Note: After steering, the UE may need to be restarted to get a new IP from the target pool.
    """
    inputs = {
        "target_dnai": {
            "type": "string",
            "description": "Target DNAI: 'edge1' for AnchorUPF1 or 'edge2' for AnchorUPF2"
        }
    }
    output_type = "string"
    
    def forward(self, target_dnai: str) -> str:
        """Create traffic influence subscription"""
        if target_dnai not in ["edge1", "edge2"]:
            return f"❌ Invalid target: {target_dnai}. Must be 'edge1' or 'edge2'"
        
        base_url = f"{CONFIG.nef_url}/3gpp-traffic-influence/v1/{CONFIG.af_id}/subscriptions"
        
        try:
            # Delete existing subscriptions
            resp = requests.get(base_url, timeout=10)
            if resp.status_code == 200:
                subs = resp.json() if resp.text else []
                for sub in subs:
                    sub_id = sub.get("self", "").split("/")[-1]
                    if sub_id:
                        requests.delete(f"{base_url}/{sub_id}", timeout=10)
            
            time.sleep(1)
            
            # Create new subscription
            payload = {
                "afServiceId": "steering",
                "afAppId": "traffic-steering-agent",
                "dnn": CONFIG.dnn,
                "snssai": {"sst": CONFIG.sst, "sd": CONFIG.sd},
                "anyUeInd": True,
                "trafficFilters": [{"flowId": 1, "flowDescriptions": ["permit out ip from any to any"]}],
                "trafficRoutes": [{"dnai": target_dnai}]
            }
            
            resp = requests.post(base_url, json=payload, timeout=10)
            
            if resp.status_code in [200, 201]:
                data = resp.json() if resp.text else {}
                sub_id = data.get("self", "").split("/")[-1]
                expected_pool = "10.1.0.0/17" if target_dnai == "edge1" else "10.1.128.0/17"
                
                return f"""✅ Traffic steering subscription created!
Target DNAI: {target_dnai}
Subscription ID: {sub_id}
Expected IP Pool: {expected_pool}

⚠️ Note: Restart the UE to apply new routing."""
            else:
                return f"❌ Failed: HTTP {resp.status_code} - {resp.text}"
                
        except Exception as e:
            return f"❌ Error: {str(e)}"


class DeleteSubscriptionsTool(Tool):
    """Tool to delete all NEF subscriptions"""
    
    name = "delete_subscriptions"
    description = "Delete all Traffic Influence subscriptions from NEF. Use to clear steering rules."
    inputs = {}
    output_type = "string"
    
    def forward(self) -> str:
        """Delete all subscriptions"""
        base_url = f"{CONFIG.nef_url}/3gpp-traffic-influence/v1/{CONFIG.af_id}/subscriptions"
        
        try:
            resp = requests.get(base_url, timeout=10)
            if resp.status_code != 200:
                return f"❌ Failed to list subscriptions: HTTP {resp.status_code}"
            
            subs = resp.json() if resp.text else []
            if not subs:
                return "No subscriptions to delete."
            
            deleted = 0
            for sub in subs:
                sub_id = sub.get("self", "").split("/")[-1]
                if sub_id:
                    del_resp = requests.delete(f"{base_url}/{sub_id}", timeout=10)
                    if del_resp.status_code in [200, 204]:
                        deleted += 1
            
            return f"✅ Deleted {deleted} subscription(s)"
        except Exception as e:
            return f"❌ Error: {str(e)}"


class GetUEStatusTool(Tool):
    """Tool to get current UE status and IP address"""
    
    name = "get_ue_status"
    description = """
    Get UE status including IP address and which UPF it's connected to.
    - 10.1.0.x = edge1 (AnchorUPF1)
    - 10.1.128.x = edge2 (AnchorUPF2)
    """
    inputs = {}
    output_type = "string"
    
    def forward(self) -> str:
        """Get UE status"""
        result = {}
        
        success, output = run_ue_command("pgrep -f nr-ue")
        result["ue_running"] = success and bool(output.strip())
        
        success, output = run_ue_command("ip addr show uesimtun0 2>/dev/null | grep 'inet '")
        
        if success and output:
            parts = output.split()
            for i, part in enumerate(parts):
                if part == "inet" and i + 1 < len(parts):
                    ip = parts[i + 1].split("/")[0]
                    result["ip_address"] = ip
                    
                    if ip.startswith("10.1.0."):
                        result["connected_to"] = "edge1 (AnchorUPF1)"
                    elif ip.startswith("10.1.128."):
                        result["connected_to"] = "edge2 (AnchorUPF2)"
                    else:
                        result["connected_to"] = "unknown"
                    break
        else:
            result["ip_address"] = None
            result["connected_to"] = "not registered"
        
        if result.get("ip_address"):
            success, _ = run_ue_command("ping -I uesimtun0 -c 1 -W 2 8.8.8.8")
            result["internet_connectivity"] = success
        else:
            result["internet_connectivity"] = False
        
        return json.dumps(result, indent=2)


class RestartUETool(Tool):
    """Tool to restart the UE"""
    
    name = "restart_ue"
    description = "Restart the UE to apply new traffic steering rules. Gets new IP from target pool."
    inputs = {}
    output_type = "string"
    
    def forward(self) -> str:
        """Restart the UE"""
        run_ue_command("sudo pkill -9 nr-ue 2>/dev/null")
        time.sleep(2)
        
        cmd = f"cd {CONFIG.ueransim_path} && sudo nohup ./build/nr-ue -c config/free5gc-ue.yaml > /tmp/ue.log 2>&1 &"
        success, output = run_ue_command(cmd)
        
        if not success:
            return f"❌ Failed to start UE: {output}"
        
        time.sleep(10)
        
        success, output = run_ue_command("ip addr show uesimtun0 2>/dev/null | grep 'inet '")
        
        if success and output:
            parts = output.split()
            for i, part in enumerate(parts):
                if part == "inet" and i + 1 < len(parts):
                    ip = parts[i + 1].split("/")[0]
                    edge = "edge1" if ip.startswith("10.1.0.") else "edge2" if ip.startswith("10.1.128.") else "unknown"
                    return f"✅ UE restarted!\nIP: {ip}\nConnected to: {edge}"
        
        return "❌ UE started but failed to register."


class GetPodStatusTool(Tool):
    """Tool to get status of 5G core pods"""
    
    name = "get_pod_status"
    description = "Get status of 5G core pods (UPFs, SMF, etc.)"
    inputs = {
        "filter": {
            "type": "string",
            "description": "Filter: 'upf', 'smf', 'nef', or 'all'"
        }
    }
    output_type = "string"
    
    def forward(self, filter: str = "all") -> str:
        """Get pod status"""
        k8s = get_k8s_client()
        pods = k8s.get_pods()
        
        if filter != "all":
            pods = [p for p in pods if filter in p["name"]]
        
        if not pods:
            return f"No pods found matching '{filter}'"
        
        result = []
        for pod in pods:
            status = "✅" if pod["ready"] else "❌"
            result.append(f"{status} {pod['name']}: {pod['status']} (restarts: {pod['restarts']})")
        
        return "\n".join(result)


class RestartPodTool(Tool):
    """Tool to restart a specific pod"""
    
    name = "restart_pod"
    description = "Restart a 5G core pod. Targets: 'smf', 'upf1', 'upf2', 'upfb', 'nef'"
    inputs = {
        "component": {
            "type": "string",
            "description": "Component to restart"
        }
    }
    output_type = "string"
    
    def forward(self, component: str) -> str:
        """Restart a pod"""
        k8s = get_k8s_client()
        pods = k8s.get_pods()
        
        matching = [p for p in pods if component in p["name"]]
        if not matching:
            return f"❌ No pod found matching '{component}'"
        
        pod_name = matching[0]["name"]
        success = k8s.delete_pod(pod_name)
        
        if success:
            return f"✅ Pod {pod_name} deleted. Will be recreated automatically."
        else:
            return f"❌ Failed to delete pod {pod_name}"


class PingTestTool(Tool):
    """Tool to run ping test from UE"""
    
    name = "ping_test"
    description = "Run ping test from UE to verify internet connectivity"
    inputs = {
        "destination": {"type": "string", "description": "Destination (default: 8.8.8.8)"},
        "count": {"type": "integer", "description": "Number of pings (default: 3)"}
    }
    output_type = "string"
    
    def forward(self, destination: str = "8.8.8.8", count: int = 3) -> str:
        """Run ping test"""
        success, output = run_ue_command(f"ping -I uesimtun0 -c {count} -W 5 {destination}")
        
        if success and "0% packet loss" in output:
            return f"✅ Ping successful!\n{output}"
        elif success:
            return f"⚠️ Ping with packet loss:\n{output}"
        else:
            return f"❌ Ping failed:\n{output}"


class CheckHealthTool(Tool):
    """Tool to run health check"""
    
    name = "check_health"
    description = "Run comprehensive health check of the traffic steering system"
    inputs = {}
    output_type = "string"
    
    def forward(self) -> str:
        """Run health check"""
        results = []
        k8s = get_k8s_client()
        
        # Check UPFs
        pods = k8s.get_pods()
        upf_pods = [p for p in pods if "upf" in p["name"]]
        upf_healthy = all(p["ready"] for p in upf_pods)
        results.append(f"{'✅' if upf_healthy else '❌'} UPF Pods: {len([p for p in upf_pods if p['ready']])}/{len(upf_pods)} ready")
        
        # Check SMF
        smf_pods = [p for p in pods if "smf" in p["name"]]
        smf_healthy = any(p["ready"] for p in smf_pods)
        results.append(f"{'✅' if smf_healthy else '❌'} SMF: {'Healthy' if smf_healthy else 'Issues'}")
        
        # Check NEF
        try:
            resp = requests.get(f"{CONFIG.nef_url}/3gpp-traffic-influence/v1/{CONFIG.af_id}/subscriptions", timeout=5)
            nef_healthy = resp.status_code == 200
        except:
            nef_healthy = False
        results.append(f"{'✅' if nef_healthy else '❌'} NEF API: {'Accessible' if nef_healthy else 'Not accessible'}")
        
        # Check UE
        success, output = run_ue_command("ip addr show uesimtun0 2>/dev/null | grep 'inet '")
        ue_healthy = success and "inet" in output
        results.append(f"{'✅' if ue_healthy else '❌'} UE: {'Registered' if ue_healthy else 'Not registered'}")
        
        all_healthy = upf_healthy and smf_healthy and nef_healthy and ue_healthy
        
        return f"""🏥 Health Check Results

{chr(10).join(results)}

{'✅ All systems healthy!' if all_healthy else '⚠️ Issues detected.'}"""


# ============================================================================
# HTTP Server for Health/API
# ============================================================================

class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for health checks and simple API"""
    
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy"}).encode())
        elif self.path == "/ready":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ready"}).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass  # Suppress logs


def start_health_server(port: int):
    """Start health check server in background"""
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health server started on port {port}")


# ============================================================================
# Main Agent Class
# ============================================================================

class TrafficSteeringAgent:
    """LLM-powered Traffic Steering Agent"""
    
    def __init__(self, model_name: str = None):
        """Initialize the agent"""
        logger.info("🤖 Initializing Traffic Steering Agent...")
        
        model_name = model_name or CONFIG.model_name
        os.environ["OLLAMA_API_BASE"] = CONFIG.ollama_base
        
        self.model = LiteLLMModel(
            model_id=f"ollama/{model_name}",
            api_base=CONFIG.ollama_base
        )
        
        self.tools = [
            GetUPFMetricsTool(),
            ListNEFSubscriptionsTool(),
            SteerTrafficTool(),
            DeleteSubscriptionsTool(),
            GetUEStatusTool(),
            RestartUETool(),
            GetPodStatusTool(),
            RestartPodTool(),
            PingTestTool(),
            CheckHealthTool(),
        ]
        
        self.agent = CodeAgent(
            tools=self.tools,
            additional_authorized_imports=["json", "time", "requests"],
            model=self.model,
            prompt_templates={
                "system_prompt": """You are an expert 5G network traffic steering agent for a free5GC ULCL deployment.

Your role is to help manage traffic steering between two anchor UPFs:
- edge1 (AnchorUPF1): IP pool 10.1.0.0/17 - UE gets IPs like 10.1.0.x
- edge2 (AnchorUPF2): IP pool 10.1.128.0/17 - UE gets IPs like 10.1.128.x

When steering traffic:
1. Check current status with list_nef_subscriptions and get_ue_status
2. Create the steering subscription with steer_traffic
3. Restart the UE with restart_ue
4. Verify the UE got the expected IP address

Always explain what you're doing and verify results.""",
            }
        )
        
        logger.info("✅ Agent initialized!")
    
    def process_request(self, user_request: str) -> str:
        """Process a user request"""
        try:
            logger.info(f"🔄 Processing: {user_request}")
            response = self.agent.run(user_request)
            return response
        except Exception as e:
            error_msg = f"❌ Error: {str(e)}"
            logger.error(error_msg)
            return error_msg


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main function"""
    print("=" * 60)
    print("🌐 5G Traffic Steering Agent (Kubernetes)")
    print("=" * 60)
    
    # Start health server
    start_health_server(CONFIG.server_port)
    
    # Initialize agent
    agent = TrafficSteeringAgent()
    
    # Example requests
    examples = [
        "Check the current system health",
        "What is the current UE status?",
        "Steer traffic to edge1",
        "Steer traffic to edge2",
        "Run a ping test",
    ]
    
    print("\n📋 Example Requests:")
    for i, req in enumerate(examples, 1):
        print(f"  {i}. {req}")
    
    print("\n" + "=" * 60)
    
    # Interactive mode
    while True:
        try:
            user_input = input("\n💬 Enter your request (or 'quit' to exit): ").strip()
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                print("👋 Goodbye!")
                break
            
            if not user_input:
                continue
            
            response = agent.process_request(user_input)
            print(f"\n🤖 Response:\n{response}")
            
        except KeyboardInterrupt:
            print("\n\n👋 Goodbye!")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")


if __name__ == "__main__":
    main()
