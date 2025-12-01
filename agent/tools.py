#!/usr/bin/env python3
"""
Traffic Steering Agent Tools
=============================
Comprehensive toolkit for managing 5G ULCL traffic steering operations.

Provides tools for:
- Kubernetes pod/deployment management
- NEF Traffic Influence API
- Prometheus metrics queries
- MongoDB operations
- UE/gNB management via SSH
- Health checks and diagnostics
"""

import os
import json
import time
import logging
import subprocess
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from enum import Enum

import requests

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
    kubeconfig: str = os.getenv("KUBECONFIG", "")
    namespace: str = os.getenv("K8S_NAMESPACE", "free5gc")
    kubectl_cmd: str = os.getenv("KUBECTL_CMD", "microk8s kubectl")
    
    # Network Function URLs
    nef_url: str = os.getenv("NEF_URL", "http://10.152.183.162:80")
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://localhost:30090")
    
    # SSH Configuration for UERANSIM VM
    ueransim_host: str = os.getenv("UERANSIM_HOST", "192.168.56.118")
    ueransim_user: str = os.getenv("UERANSIM_USER", "vagrant")
    ueransim_key: str = os.getenv("UERANSIM_KEY", "")
    ueransim_path: str = os.getenv("UERANSIM_PATH", "~/ue/UERANSIM")
    
    # Vagrant (for local development)
    vagrant_dir: str = os.getenv("VAGRANT_DIR", "/home/elliot/Documents/traffic-steering/vagrant")
    use_vagrant: bool = os.getenv("USE_VAGRANT", "true").lower() == "true"
    
    # Traffic Steering
    af_id: str = os.getenv("AF_ID", "traffic-steering-agent")
    dnn: str = os.getenv("DNN", "internet")
    sst: int = int(os.getenv("SST", "1"))
    sd: str = os.getenv("SD", "010203")
    
    # DNAI Configuration
    edge1_dnai: str = "edge1"
    edge2_dnai: str = "edge2"
    edge1_pool: str = "10.1.0.0/17"
    edge2_pool: str = "10.1.128.0/17"


class SteeringTarget(Enum):
    """Traffic steering targets"""
    EDGE1 = "edge1"  # AnchorUPF1 - 10.1.0.0/17
    EDGE2 = "edge2"  # AnchorUPF2 - 10.1.128.0/17


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class PodInfo:
    """Kubernetes pod information"""
    name: str
    status: str
    ready: bool
    restarts: int
    age: str
    node: str = ""
    ip: str = ""


@dataclass
class UPFMetrics:
    """UPF traffic metrics"""
    pod_name: str
    tx_rate_mbps: float = 0.0
    rx_rate_mbps: float = 0.0
    
    @property
    def total_rate_mbps(self) -> float:
        return self.tx_rate_mbps + self.rx_rate_mbps


@dataclass 
class TrafficInfluenceSubscription:
    """NEF Traffic Influence subscription"""
    subscription_id: str
    af_service_id: str
    af_app_id: str
    dnn: str
    snssai: Dict[str, Any]
    dnai: str
    any_ue_ind: bool = True


@dataclass
class UEStatus:
    """UE registration and connectivity status"""
    registered: bool
    ip_address: str
    pdu_session_active: bool
    connected_upf: str  # "edge1" or "edge2" based on IP


@dataclass
class OperationResult:
    """Result of an operation"""
    success: bool
    message: str
    data: Any = None
    error: Optional[str] = None


# ============================================================================
# Kubernetes Tools
# ============================================================================

class KubernetesTools:
    """Tools for Kubernetes operations"""
    
    def __init__(self, config: AgentConfig):
        self.config = config
        self.kubectl = config.kubectl_cmd
        self.namespace = config.namespace
        
    def _run_kubectl(self, args: str, timeout: int = 30) -> Tuple[bool, str]:
        """Execute kubectl command"""
        cmd = f"{self.kubectl} -n {self.namespace} {args}"
        
        if self.config.use_vagrant:
            cmd = f"cd {self.config.vagrant_dir} && vagrant ssh ns -c '{cmd}'"
        
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
            else:
                return False, result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)
    
    def get_pods(self, label_selector: str = "") -> OperationResult:
        """Get pods in namespace"""
        args = "get pods -o wide"
        if label_selector:
            args += f" -l {label_selector}"
        
        success, output = self._run_kubectl(args)
        if not success:
            return OperationResult(False, "Failed to get pods", error=output)
        
        pods = []
        lines = output.strip().split('\n')
        if len(lines) > 1:
            for line in lines[1:]:  # Skip header
                parts = line.split()
                if len(parts) >= 5:
                    ready_parts = parts[1].split('/')
                    pods.append(PodInfo(
                        name=parts[0],
                        ready=ready_parts[0] == ready_parts[1],
                        status=parts[2],
                        restarts=int(parts[3].split()[0]) if parts[3][0].isdigit() else 0,
                        age=parts[4],
                        ip=parts[5] if len(parts) > 5 else "",
                        node=parts[6] if len(parts) > 6 else ""
                    ))
        
        return OperationResult(True, f"Found {len(pods)} pods", data=pods)
    
    def get_upf_pods(self) -> OperationResult:
        """Get all UPF pods"""
        success, output = self._run_kubectl("get pods | grep upf")
        if not success:
            return OperationResult(False, "Failed to get UPF pods", error=output)
        
        pods = []
        for line in output.strip().split('\n'):
            if line:
                parts = line.split()
                if len(parts) >= 3:
                    pods.append(PodInfo(
                        name=parts[0],
                        ready=parts[1] == "1/1",
                        status=parts[2],
                        restarts=int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0,
                        age=parts[4] if len(parts) > 4 else ""
                    ))
        
        return OperationResult(True, f"Found {len(pods)} UPF pods", data=pods)
    
    def get_smf_pods(self) -> OperationResult:
        """Get SMF pods"""
        success, output = self._run_kubectl("get pods | grep smf")
        if not success:
            return OperationResult(False, "Failed to get SMF pods", error=output)
        
        pods = []
        for line in output.strip().split('\n'):
            if line:
                parts = line.split()
                if len(parts) >= 3:
                    pods.append(PodInfo(
                        name=parts[0],
                        ready=parts[1] == "1/1",
                        status=parts[2],
                        restarts=int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0,
                        age=parts[4] if len(parts) > 4 else ""
                    ))
        
        return OperationResult(True, f"Found {len(pods)} SMF pods", data=pods)
    
    def delete_pod(self, pod_name: str, force: bool = True) -> OperationResult:
        """Delete a pod (triggers restart via deployment)"""
        args = f"delete pod {pod_name}"
        if force:
            args += " --force --grace-period=0"
        
        logger.info(f"Deleting pod: {pod_name}")
        success, output = self._run_kubectl(args, timeout=60)
        
        if success or "deleted" in output.lower():
            return OperationResult(True, f"Pod {pod_name} deleted")
        return OperationResult(False, f"Failed to delete pod {pod_name}", error=output)
    
    def restart_deployment(self, deployment_name: str) -> OperationResult:
        """Restart a deployment by rolling restart"""
        args = f"rollout restart deployment/{deployment_name}"
        
        logger.info(f"Restarting deployment: {deployment_name}")
        success, output = self._run_kubectl(args)
        
        if success:
            return OperationResult(True, f"Deployment {deployment_name} restart initiated")
        return OperationResult(False, f"Failed to restart deployment", error=output)
    
    def restart_smf(self) -> OperationResult:
        """Restart SMF deployment"""
        return self.restart_deployment("free5gc-v1-free5gc-smf-smf")
    
    def restart_upf(self, upf_name: str) -> OperationResult:
        """Restart specific UPF by deleting its pod"""
        # Map UPF names to deployment patterns
        upf_map = {
            "upf1": "upf-upf1",
            "upf2": "upf-upf2", 
            "upfb": "upf-upfb",
            "upfb2": "upf-upfb2",
            "AnchorUPF1": "upf-upf1",
            "AnchorUPF2": "upf-upf2",
            "BranchingUPF1": "upf-upfb",
            "BranchingUPF2": "upf-upfb2"
        }
        
        pattern = upf_map.get(upf_name, upf_name)
        
        # Find the pod
        result = self.get_upf_pods()
        if not result.success:
            return result
        
        for pod in result.data:
            if pattern in pod.name:
                return self.delete_pod(pod.name)
        
        return OperationResult(False, f"UPF pod not found for: {upf_name}")
    
    def restart_all_upfs(self) -> OperationResult:
        """Restart all UPF pods"""
        result = self.get_upf_pods()
        if not result.success:
            return result
        
        restarted = []
        failed = []
        
        for pod in result.data:
            delete_result = self.delete_pod(pod.name)
            if delete_result.success:
                restarted.append(pod.name)
            else:
                failed.append(pod.name)
        
        if failed:
            return OperationResult(
                False, 
                f"Some UPFs failed to restart: {failed}",
                data={"restarted": restarted, "failed": failed}
            )
        
        return OperationResult(True, f"Restarted {len(restarted)} UPF pods", data=restarted)
    
    def scale_replicaset(self, rs_name: str, replicas: int) -> OperationResult:
        """Scale a ReplicaSet"""
        args = f"scale rs {rs_name} --replicas={replicas}"
        
        logger.info(f"Scaling ReplicaSet {rs_name} to {replicas}")
        success, output = self._run_kubectl(args)
        
        if success:
            return OperationResult(True, f"ReplicaSet {rs_name} scaled to {replicas}")
        return OperationResult(False, f"Failed to scale ReplicaSet", error=output)
    
    def wait_for_pod_ready(self, pod_selector: str, timeout: int = 120) -> OperationResult:
        """Wait for pod to be ready"""
        args = f"wait --for=condition=ready pod -l {pod_selector} --timeout={timeout}s"
        
        logger.info(f"Waiting for pod with selector: {pod_selector}")
        success, output = self._run_kubectl(args, timeout=timeout + 10)
        
        if success:
            return OperationResult(True, "Pod is ready")
        return OperationResult(False, "Pod not ready within timeout", error=output)
    
    def get_pod_logs(self, pod_name: str, lines: int = 50, grep_pattern: str = "") -> OperationResult:
        """Get pod logs"""
        args = f"logs {pod_name} --tail={lines}"
        
        success, output = self._run_kubectl(args, timeout=30)
        if not success:
            return OperationResult(False, "Failed to get logs", error=output)
        
        if grep_pattern:
            lines = [l for l in output.split('\n') if grep_pattern.lower() in l.lower()]
            output = '\n'.join(lines)
        
        return OperationResult(True, "Logs retrieved", data=output)
    
    def check_upf_associations(self) -> OperationResult:
        """Check SMF-UPF PFCP associations"""
        result = self.get_smf_pods()
        if not result.success or not result.data:
            return OperationResult(False, "No SMF pods found")
        
        smf_pod = result.data[0].name
        log_result = self.get_pod_logs(smf_pod, lines=200, grep_pattern="association")
        
        if not log_result.success:
            return log_result
        
        associations = []
        for line in log_result.data.split('\n'):
            if 'association' in line.lower():
                associations.append(line)
        
        return OperationResult(
            True,
            f"Found {len(associations)} association-related log entries",
            data=associations
        )
    
    def get_nef_service_ip(self) -> OperationResult:
        """Get NEF service ClusterIP"""
        success, output = self._run_kubectl("get svc | grep nef")
        
        if not success:
            return OperationResult(False, "Failed to get NEF service", error=output)
        
        parts = output.split()
        if len(parts) >= 3:
            return OperationResult(True, "NEF service found", data=parts[2])
        
        return OperationResult(False, "Could not parse NEF service IP")


# ============================================================================
# NEF Traffic Influence Tools
# ============================================================================

class NEFTools:
    """Tools for NEF Traffic Influence API operations"""
    
    def __init__(self, config: AgentConfig):
        self.config = config
        self.base_url = f"{config.nef_url}/3gpp-traffic-influence/v1/{config.af_id}/subscriptions"
    
    def _make_request(self, method: str, url: str, json_data: Dict = None) -> Tuple[bool, Any]:
        """Make HTTP request to NEF"""
        try:
            if method == "GET":
                resp = requests.get(url, timeout=10)
            elif method == "POST":
                resp = requests.post(url, json=json_data, timeout=10)
            elif method == "PUT":
                resp = requests.put(url, json=json_data, timeout=10)
            elif method == "DELETE":
                resp = requests.delete(url, timeout=10)
            else:
                return False, f"Unknown method: {method}"
            
            if resp.status_code in [200, 201, 204]:
                try:
                    return True, resp.json() if resp.text else {}
                except:
                    return True, {}
            else:
                return False, f"HTTP {resp.status_code}: {resp.text}"
                
        except requests.exceptions.ConnectionError:
            return False, "Connection refused - is NEF running?"
        except Exception as e:
            return False, str(e)
    
    def list_subscriptions(self) -> OperationResult:
        """List all traffic influence subscriptions"""
        success, data = self._make_request("GET", self.base_url)
        
        if not success:
            return OperationResult(False, "Failed to list subscriptions", error=str(data))
        
        subscriptions = []
        for sub in data if isinstance(data, list) else []:
            self_link = sub.get("self", "")
            sub_id = self_link.split("/")[-1] if self_link else ""
            
            dnai = ""
            routes = sub.get("trafficRoutes", [])
            if routes:
                dnai = routes[0].get("dnai", "")
            
            subscriptions.append(TrafficInfluenceSubscription(
                subscription_id=sub_id,
                af_service_id=sub.get("afServiceId", ""),
                af_app_id=sub.get("afAppId", ""),
                dnn=sub.get("dnn", ""),
                snssai=sub.get("snssai", {}),
                dnai=dnai,
                any_ue_ind=sub.get("anyUeInd", False)
            ))
        
        return OperationResult(True, f"Found {len(subscriptions)} subscriptions", data=subscriptions)
    
    def get_subscription(self, subscription_id: str) -> OperationResult:
        """Get a specific subscription"""
        url = f"{self.base_url}/{subscription_id}"
        success, data = self._make_request("GET", url)
        
        if not success:
            return OperationResult(False, f"Failed to get subscription {subscription_id}", error=str(data))
        
        return OperationResult(True, "Subscription retrieved", data=data)
    
    def create_subscription(self, target_dnai: str, flow_description: str = "permit out ip from any to any") -> OperationResult:
        """Create a traffic influence subscription"""
        payload = {
            "afServiceId": "steering",
            "afAppId": "traffic-steering-agent",
            "dnn": self.config.dnn,
            "snssai": {
                "sst": self.config.sst,
                "sd": self.config.sd
            },
            "anyUeInd": True,
            "trafficFilters": [{
                "flowId": 1,
                "flowDescriptions": [flow_description]
            }],
            "trafficRoutes": [{
                "dnai": target_dnai
            }]
        }
        
        logger.info(f"Creating traffic influence subscription for DNAI: {target_dnai}")
        success, data = self._make_request("POST", self.base_url, payload)
        
        if not success:
            return OperationResult(False, "Failed to create subscription", error=str(data))
        
        # Extract subscription ID
        self_link = data.get("self", "") if isinstance(data, dict) else ""
        sub_id = self_link.split("/")[-1] if self_link else ""
        
        return OperationResult(
            True, 
            f"Subscription created for {target_dnai}",
            data={"subscription_id": sub_id, "dnai": target_dnai}
        )
    
    def delete_subscription(self, subscription_id: str) -> OperationResult:
        """Delete a subscription"""
        url = f"{self.base_url}/{subscription_id}"
        
        logger.info(f"Deleting subscription: {subscription_id}")
        success, data = self._make_request("DELETE", url)
        
        if not success:
            return OperationResult(False, f"Failed to delete subscription {subscription_id}", error=str(data))
        
        return OperationResult(True, f"Subscription {subscription_id} deleted")
    
    def delete_all_subscriptions(self) -> OperationResult:
        """Delete all subscriptions"""
        list_result = self.list_subscriptions()
        if not list_result.success:
            return list_result
        
        deleted = []
        failed = []
        
        for sub in list_result.data:
            result = self.delete_subscription(sub.subscription_id)
            if result.success:
                deleted.append(sub.subscription_id)
            else:
                failed.append(sub.subscription_id)
        
        if failed:
            return OperationResult(
                False,
                f"Some subscriptions failed to delete",
                data={"deleted": deleted, "failed": failed}
            )
        
        return OperationResult(True, f"Deleted {len(deleted)} subscriptions", data=deleted)
    
    def steer_to_edge1(self) -> OperationResult:
        """Steer traffic to edge1 (AnchorUPF1)"""
        # First delete existing subscriptions
        self.delete_all_subscriptions()
        time.sleep(1)
        
        return self.create_subscription(self.config.edge1_dnai)
    
    def steer_to_edge2(self) -> OperationResult:
        """Steer traffic to edge2 (AnchorUPF2)"""
        # First delete existing subscriptions
        self.delete_all_subscriptions()
        time.sleep(1)
        
        return self.create_subscription(self.config.edge2_dnai)
    
    def steer_to(self, target: SteeringTarget) -> OperationResult:
        """Steer traffic to specified target"""
        if target == SteeringTarget.EDGE1:
            return self.steer_to_edge1()
        else:
            return self.steer_to_edge2()


# ============================================================================
# Prometheus Tools
# ============================================================================

class PrometheusTools:
    """Tools for Prometheus metrics queries"""
    
    def __init__(self, config: AgentConfig):
        self.config = config
        self.base_url = f"{config.prometheus_url}/api/v1"
    
    def query(self, promql: str) -> OperationResult:
        """Execute a PromQL query"""
        try:
            resp = requests.get(
                f"{self.base_url}/query",
                params={"query": promql},
                timeout=10
            )
            
            if resp.status_code != 200:
                return OperationResult(False, f"Query failed: HTTP {resp.status_code}", error=resp.text)
            
            data = resp.json()
            if data.get("status") != "success":
                return OperationResult(False, "Query returned error", error=data.get("error"))
            
            return OperationResult(True, "Query successful", data=data.get("data", {}))
            
        except requests.exceptions.ConnectionError:
            return OperationResult(False, "Cannot connect to Prometheus")
        except Exception as e:
            return OperationResult(False, "Query failed", error=str(e))
    
    def get_upf_traffic_rate(self, pod_pattern: str, interface: str = "n6") -> OperationResult:
        """Get traffic rate for a UPF pod"""
        tx_query = f'rate(container_network_transmit_bytes_total{{namespace="free5gc",pod=~"{pod_pattern}",interface="{interface}"}}[1m]) * 8 / 1000000'
        rx_query = f'rate(container_network_receive_bytes_total{{namespace="free5gc",pod=~"{pod_pattern}",interface="{interface}"}}[1m]) * 8 / 1000000'
        
        tx_result = self.query(tx_query)
        rx_result = self.query(rx_query)
        
        tx_rate = 0.0
        rx_rate = 0.0
        pod_name = "unknown"
        
        if tx_result.success and tx_result.data.get("result"):
            result = tx_result.data["result"][0]
            pod_name = result.get("metric", {}).get("pod", "unknown")
            tx_rate = float(result["value"][1])
        
        if rx_result.success and rx_result.data.get("result"):
            rx_rate = float(rx_result.data["result"][0]["value"][1])
        
        metrics = UPFMetrics(pod_name=pod_name, tx_rate_mbps=tx_rate, rx_rate_mbps=rx_rate)
        
        return OperationResult(True, f"Metrics for {pod_name}", data=metrics)
    
    def get_upf1_metrics(self) -> OperationResult:
        """Get AnchorUPF1 metrics"""
        return self.get_upf_traffic_rate(".*upf1.*", "n6")
    
    def get_upf2_metrics(self) -> OperationResult:
        """Get AnchorUPF2 metrics"""
        return self.get_upf_traffic_rate(".*upf2.*", "n6")
    
    def get_all_upf_metrics(self) -> OperationResult:
        """Get metrics for all UPFs"""
        metrics = {}
        
        for upf in ["upf1", "upf2", "upfb", "upfb2"]:
            result = self.get_upf_traffic_rate(f".*{upf}.*")
            if result.success:
                metrics[upf] = result.data
        
        return OperationResult(True, f"Metrics for {len(metrics)} UPFs", data=metrics)


# ============================================================================
# UERANSIM Tools
# ============================================================================

class UERANSIMTools:
    """Tools for UERANSIM UE/gNB management"""
    
    def __init__(self, config: AgentConfig):
        self.config = config
    
    def _run_ssh_command(self, command: str, timeout: int = 30) -> Tuple[bool, str]:
        """Run command on UERANSIM VM"""
        if self.config.use_vagrant:
            cmd = f"cd {self.config.vagrant_dir} && vagrant ssh vm3 -c \"{command}\""
        else:
            ssh_key = f"-i {self.config.ueransim_key}" if self.config.ueransim_key else ""
            cmd = f"ssh {ssh_key} {self.config.ueransim_user}@{self.config.ueransim_host} \"{command}\""
        
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.returncode == 0, result.stdout.strip() or result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)
    
    def get_ue_ip(self) -> OperationResult:
        """Get UE tunnel IP address"""
        success, output = self._run_ssh_command("ip addr show uesimtun0 2>/dev/null | grep 'inet '")
        
        if not success or not output:
            return OperationResult(False, "UE interface not found - UE may not be registered")
        
        # Parse IP from output like "inet 10.1.0.1/24 scope global uesimtun0"
        parts = output.split()
        for i, part in enumerate(parts):
            if part == "inet" and i + 1 < len(parts):
                ip = parts[i + 1].split("/")[0]
                
                # Determine which UPF based on IP
                if ip.startswith("10.1.0."):
                    upf = "edge1 (AnchorUPF1)"
                elif ip.startswith("10.1.128."):
                    upf = "edge2 (AnchorUPF2)"
                else:
                    upf = "unknown"
                
                return OperationResult(True, f"UE IP: {ip} ({upf})", data={"ip": ip, "upf": upf})
        
        return OperationResult(False, "Could not parse UE IP")
    
    def get_ue_status(self) -> OperationResult:
        """Get comprehensive UE status"""
        # Check if UE process is running
        success, output = self._run_ssh_command("pgrep -f nr-ue")
        ue_running = success and output.strip()
        
        # Get IP if registered
        ip_result = self.get_ue_ip()
        
        # Check connectivity
        connectivity = False
        if ip_result.success:
            ping_success, _ = self._run_ssh_command("ping -I uesimtun0 -c 1 -W 2 8.8.8.8")
            connectivity = ping_success
        
        status = UEStatus(
            registered=ip_result.success,
            ip_address=ip_result.data.get("ip", "") if ip_result.success else "",
            pdu_session_active=ip_result.success,
            connected_upf=ip_result.data.get("upf", "") if ip_result.success else ""
        )
        
        return OperationResult(
            True,
            f"UE running: {ue_running}, Registered: {status.registered}, Connectivity: {connectivity}",
            data={"status": status, "ue_running": ue_running, "connectivity": connectivity}
        )
    
    def start_ue(self) -> OperationResult:
        """Start UE"""
        logger.info("Starting UE...")
        
        # Kill existing UE first
        self._run_ssh_command("sudo pkill -9 nr-ue 2>/dev/null")
        time.sleep(2)
        
        # Start UE
        cmd = f"cd {self.config.ueransim_path} && sudo nohup ./build/nr-ue -c config/free5gc-ue.yaml > /tmp/ue.log 2>&1 &"
        success, output = self._run_ssh_command(cmd)
        
        if not success:
            return OperationResult(False, "Failed to start UE", error=output)
        
        # Wait for registration
        time.sleep(10)
        
        # Check status
        return self.get_ue_status()
    
    def stop_ue(self) -> OperationResult:
        """Stop UE"""
        logger.info("Stopping UE...")
        success, output = self._run_ssh_command("sudo pkill -9 nr-ue 2>/dev/null")
        
        time.sleep(2)
        
        # Verify stopped
        check_success, _ = self._run_ssh_command("pgrep -f nr-ue")
        if check_success:
            return OperationResult(False, "UE still running after stop command")
        
        return OperationResult(True, "UE stopped")
    
    def restart_ue(self) -> OperationResult:
        """Restart UE (stop then start)"""
        logger.info("Restarting UE...")
        
        stop_result = self.stop_ue()
        if not stop_result.success:
            logger.warning(f"Stop failed: {stop_result.message}")
        
        return self.start_ue()
    
    def get_ue_logs(self, lines: int = 20) -> OperationResult:
        """Get UE logs"""
        success, output = self._run_ssh_command(f"tail -{lines} /tmp/ue.log 2>/dev/null")
        
        if not success:
            return OperationResult(False, "Failed to get UE logs", error=output)
        
        return OperationResult(True, "UE logs retrieved", data=output)
    
    def ping_test(self, destination: str = "8.8.8.8", count: int = 3) -> OperationResult:
        """Run ping test from UE"""
        success, output = self._run_ssh_command(f"ping -I uesimtun0 -c {count} -W 5 {destination}")
        
        # Parse ping statistics
        packet_loss = "100%"
        if "packet loss" in output:
            for line in output.split('\n'):
                if "packet loss" in line:
                    parts = line.split(',')
                    for part in parts:
                        if "packet loss" in part:
                            packet_loss = part.strip().split()[0]
        
        return OperationResult(
            success and "0% packet loss" in output,
            f"Ping test: {packet_loss} packet loss",
            data={"output": output, "packet_loss": packet_loss}
        )


# ============================================================================
# Health Check Tools  
# ============================================================================

class HealthCheckTools:
    """Tools for system health checks"""
    
    def __init__(self, config: AgentConfig):
        self.config = config
        self.k8s = KubernetesTools(config)
        self.nef = NEFTools(config)
        self.ue = UERANSIMTools(config)
    
    def check_upf_health(self) -> OperationResult:
        """Check health of all UPFs"""
        result = self.k8s.get_upf_pods()
        if not result.success:
            return result
        
        healthy = []
        unhealthy = []
        
        for pod in result.data:
            if pod.status == "Running" and pod.ready:
                healthy.append(pod.name)
            else:
                unhealthy.append({"name": pod.name, "status": pod.status, "ready": pod.ready})
        
        all_healthy = len(unhealthy) == 0
        
        return OperationResult(
            all_healthy,
            f"UPFs: {len(healthy)} healthy, {len(unhealthy)} unhealthy",
            data={"healthy": healthy, "unhealthy": unhealthy}
        )
    
    def check_smf_health(self) -> OperationResult:
        """Check SMF health"""
        result = self.k8s.get_smf_pods()
        if not result.success:
            return result
        
        if not result.data:
            return OperationResult(False, "No SMF pods found")
        
        pod = result.data[0]
        healthy = pod.status == "Running" and pod.ready
        
        return OperationResult(
            healthy,
            f"SMF: {pod.status}, Ready: {pod.ready}",
            data=pod
        )
    
    def check_nef_connectivity(self) -> OperationResult:
        """Check NEF API connectivity"""
        result = self.nef.list_subscriptions()
        return OperationResult(
            result.success,
            "NEF API accessible" if result.success else "NEF API not accessible",
            error=result.error
        )
    
    def check_ue_connectivity(self) -> OperationResult:
        """Check UE registration and connectivity"""
        return self.ue.get_ue_status()
    
    def run_full_health_check(self) -> OperationResult:
        """Run comprehensive health check"""
        checks = {
            "upf": self.check_upf_health(),
            "smf": self.check_smf_health(),
            "nef": self.check_nef_connectivity(),
            "ue": self.check_ue_connectivity()
        }
        
        all_healthy = all(c.success for c in checks.values())
        
        summary = {
            name: {"healthy": result.success, "message": result.message}
            for name, result in checks.items()
        }
        
        return OperationResult(
            all_healthy,
            "All systems healthy" if all_healthy else "Some systems unhealthy",
            data=summary
        )


# ============================================================================
# Main Agent Toolkit
# ============================================================================

class TrafficSteeringToolkit:
    """Complete toolkit for traffic steering operations"""
    
    def __init__(self, config: AgentConfig = None):
        self.config = config or AgentConfig()
        
        # Initialize all tool classes
        self.k8s = KubernetesTools(self.config)
        self.nef = NEFTools(self.config)
        self.prometheus = PrometheusTools(self.config)
        self.ue = UERANSIMTools(self.config)
        self.health = HealthCheckTools(self.config)
        
        # Track current state
        self.current_dnai: Optional[str] = None
        self.current_subscription_id: Optional[str] = None
    
    # ---- High-Level Operations ----
    
    def steer_traffic(self, target: SteeringTarget) -> OperationResult:
        """
        Complete traffic steering operation:
        1. Delete old subscriptions
        2. Create new subscription for target DNAI
        3. Restart UE to apply new routing
        4. Verify UE got correct IP
        """
        logger.info(f"=== Steering traffic to {target.value} ===")
        
        # Step 1: Create subscription
        sub_result = self.nef.steer_to(target)
        if not sub_result.success:
            return sub_result
        
        self.current_dnai = target.value
        self.current_subscription_id = sub_result.data.get("subscription_id")
        
        # Step 2: Restart UE
        ue_result = self.ue.restart_ue()
        if not ue_result.success:
            return OperationResult(
                False,
                "Subscription created but UE restart failed",
                data={"subscription": sub_result.data},
                error=ue_result.error
            )
        
        # Step 3: Verify IP
        time.sleep(2)
        ip_result = self.ue.get_ue_ip()
        
        if ip_result.success:
            expected_prefix = "10.1.0." if target == SteeringTarget.EDGE1 else "10.1.128."
            actual_ip = ip_result.data.get("ip", "")
            
            if actual_ip.startswith(expected_prefix):
                return OperationResult(
                    True,
                    f"Traffic steered to {target.value}, UE IP: {actual_ip}",
                    data={"dnai": target.value, "ip": actual_ip}
                )
            else:
                return OperationResult(
                    False,
                    f"UE got unexpected IP {actual_ip} (expected {expected_prefix}x)",
                    data={"dnai": target.value, "ip": actual_ip}
                )
        
        return OperationResult(
            False,
            "Could not verify UE IP after steering",
            error=ip_result.error
        )
    
    def ensure_system_ready(self) -> OperationResult:
        """
        Ensure all components are ready for traffic steering:
        1. Check UPF health, restart if needed
        2. Check SMF health, restart if needed  
        3. Verify NEF connectivity
        """
        logger.info("=== Ensuring system readiness ===")
        
        # Check and fix UPFs
        upf_result = self.health.check_upf_health()
        if not upf_result.success and upf_result.data:
            unhealthy = upf_result.data.get("unhealthy", [])
            for upf in unhealthy:
                logger.warning(f"Restarting unhealthy UPF: {upf['name']}")
                self.k8s.delete_pod(upf['name'])
            
            # Wait for recovery
            time.sleep(30)
            upf_result = self.health.check_upf_health()
        
        # Check and fix SMF
        smf_result = self.health.check_smf_health()
        if not smf_result.success:
            logger.warning("Restarting SMF...")
            self.k8s.restart_smf()
            time.sleep(30)
            smf_result = self.health.check_smf_health()
        
        # Final health check
        return self.health.run_full_health_check()
    
    def get_current_steering_state(self) -> OperationResult:
        """Get current traffic steering state"""
        # Get active subscriptions
        sub_result = self.nef.list_subscriptions()
        
        # Get UE status
        ue_result = self.ue.get_ue_status()
        
        current_dnai = None
        if sub_result.success and sub_result.data:
            current_dnai = sub_result.data[0].dnai
        
        ue_ip = None
        connected_upf = None
        if ue_result.success and ue_result.data:
            status = ue_result.data.get("status")
            if status:
                ue_ip = status.ip_address
                connected_upf = status.connected_upf
        
        return OperationResult(
            True,
            f"Current DNAI: {current_dnai}, UE connected to: {connected_upf}",
            data={
                "subscription_dnai": current_dnai,
                "ue_ip": ue_ip,
                "connected_upf": connected_upf,
                "subscriptions": [s.__dict__ for s in sub_result.data] if sub_result.data else []
            }
        )


# ============================================================================
# CLI Interface
# ============================================================================

def main():
    """CLI interface for traffic steering tools"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Traffic Steering Agent Tools")
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # Steering commands
    steer_parser = subparsers.add_parser("steer", help="Steer traffic")
    steer_parser.add_argument("target", choices=["edge1", "edge2"], help="Target DNAI")
    
    # Status commands
    subparsers.add_parser("status", help="Get current steering status")
    subparsers.add_parser("health", help="Run health check")
    
    # Subscription commands
    subparsers.add_parser("list-subs", help="List NEF subscriptions")
    subparsers.add_parser("clear-subs", help="Delete all subscriptions")
    
    # UE commands
    subparsers.add_parser("ue-status", help="Get UE status")
    subparsers.add_parser("ue-restart", help="Restart UE")
    subparsers.add_parser("ue-ping", help="Run ping test from UE")
    
    # K8s commands
    subparsers.add_parser("pods", help="List UPF/SMF pods")
    restart_parser = subparsers.add_parser("restart", help="Restart component")
    restart_parser.add_argument("component", choices=["smf", "upf1", "upf2", "upfb", "all-upfs"])
    
    args = parser.parse_args()
    
    toolkit = TrafficSteeringToolkit()
    
    if args.command == "steer":
        target = SteeringTarget.EDGE1 if args.target == "edge1" else SteeringTarget.EDGE2
        result = toolkit.steer_traffic(target)
    elif args.command == "status":
        result = toolkit.get_current_steering_state()
    elif args.command == "health":
        result = toolkit.health.run_full_health_check()
    elif args.command == "list-subs":
        result = toolkit.nef.list_subscriptions()
    elif args.command == "clear-subs":
        result = toolkit.nef.delete_all_subscriptions()
    elif args.command == "ue-status":
        result = toolkit.ue.get_ue_status()
    elif args.command == "ue-restart":
        result = toolkit.ue.restart_ue()
    elif args.command == "ue-ping":
        result = toolkit.ue.ping_test()
    elif args.command == "pods":
        result = toolkit.k8s.get_upf_pods()
        if result.success:
            print("\nUPF Pods:")
            for pod in result.data:
                status = "✓" if pod.ready else "✗"
                print(f"  {status} {pod.name}: {pod.status} (restarts: {pod.restarts})")
        
        smf_result = toolkit.k8s.get_smf_pods()
        if smf_result.success:
            print("\nSMF Pods:")
            for pod in smf_result.data:
                status = "✓" if pod.ready else "✗"
                print(f"  {status} {pod.name}: {pod.status}")
        return
    elif args.command == "restart":
        if args.component == "smf":
            result = toolkit.k8s.restart_smf()
        elif args.component == "all-upfs":
            result = toolkit.k8s.restart_all_upfs()
        else:
            result = toolkit.k8s.restart_upf(args.component)
    else:
        parser.print_help()
        return
    
    # Print result
    status = "✓" if result.success else "✗"
    print(f"\n{status} {result.message}")
    
    if result.data:
        print(f"\nData: {json.dumps(result.data, indent=2, default=str)}")
    
    if result.error:
        print(f"\nError: {result.error}")


if __name__ == "__main__":
    main()
