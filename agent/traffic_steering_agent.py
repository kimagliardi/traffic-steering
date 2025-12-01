#!/usr/bin/env python3
"""
Dynamic Traffic Steering Agent for 5G ULCL
==========================================
Monitors Prometheus metrics for UPF traffic and dynamically steers
traffic between anchor UPFs based on load thresholds.

This agent:
1. Polls Prometheus for UPF network traffic metrics
2. Compares traffic on edge1 (AnchorUPF1) vs edge2 (AnchorUPF2)
3. If one UPF is overloaded, steers new traffic flows to the other

Based on 3GPP Traffic Influence API via NEF
"""

import os
import time
import json
import logging
import requests
from dataclasses import dataclass
from typing import Optional, Dict, Any

# Configuration
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:30090")
NEF_URL = os.getenv("NEF_URL", "http://localhost:30060")
AF_ID = os.getenv("AF_ID", "traffic-steering-agent")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # seconds
THRESHOLD_MBPS = float(os.getenv("THRESHOLD_MBPS", "1.0"))  # threshold for steering
DNN = os.getenv("DNN", "internet")
SST = int(os.getenv("SST", "1"))
SD = os.getenv("SD", "010203")

# UPF Pod name patterns for Prometheus queries
UPF1_POD_PATTERN = ".*upf1.*"
UPF2_POD_PATTERN = ".*upf2.*"

# DNAI mappings (must match SMF config)
EDGE1_DNAI = "edge1"  # AnchorUPF1
EDGE2_DNAI = "edge2"  # AnchorUPF2

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class UPFMetrics:
    """Holds metrics for a UPF"""
    pod_name: str
    tx_rate_mbps: float
    rx_rate_mbps: float
    
    @property
    def total_rate_mbps(self) -> float:
        return self.tx_rate_mbps + self.rx_rate_mbps


class TrafficSteeringAgent:
    """Agent that monitors UPF metrics and steers traffic dynamically"""
    
    def __init__(self):
        self.prometheus_url = PROMETHEUS_URL
        self.nef_url = NEF_URL
        self.af_id = AF_ID
        self.current_dnai = EDGE1_DNAI  # Start with edge1
        self.subscription_id: Optional[str] = None
        
    def get_upf_metrics(self, pod_pattern: str, interface: str = "n6") -> Optional[UPFMetrics]:
        """
        Query Prometheus for UPF traffic metrics
        
        Args:
            pod_pattern: Regex pattern for pod name (e.g., ".*upf1.*")
            interface: Network interface to monitor (n3, n6, n9, etc.)
        
        Returns:
            UPFMetrics or None if query fails
        """
        # Query for TX rate (bytes/sec -> Mbps)
        tx_query = f'rate(container_network_transmit_bytes_total{{namespace="free5gc",pod=~"{pod_pattern}",interface="{interface}"}}[1m]) * 8 / 1000000'
        # Query for RX rate
        rx_query = f'rate(container_network_receive_bytes_total{{namespace="free5gc",pod=~"{pod_pattern}",interface="{interface}"}}[1m]) * 8 / 1000000'
        
        try:
            # Get TX rate
            resp = requests.get(
                f"{self.prometheus_url}/api/v1/query",
                params={"query": tx_query},
                timeout=5
            )
            tx_data = resp.json()
            tx_rate = 0.0
            pod_name = "unknown"
            
            if tx_data.get("status") == "success" and tx_data.get("data", {}).get("result"):
                result = tx_data["data"]["result"][0]
                pod_name = result.get("metric", {}).get("pod", "unknown")
                tx_rate = float(result["value"][1])
            
            # Get RX rate
            resp = requests.get(
                f"{self.prometheus_url}/api/v1/query",
                params={"query": rx_query},
                timeout=5
            )
            rx_data = resp.json()
            rx_rate = 0.0
            
            if rx_data.get("status") == "success" and rx_data.get("data", {}).get("result"):
                rx_rate = float(rx_data["data"]["result"][0]["value"][1])
            
            return UPFMetrics(pod_name=pod_name, tx_rate_mbps=tx_rate, rx_rate_mbps=rx_rate)
            
        except Exception as e:
            logger.error(f"Failed to query Prometheus: {e}")
            return None
    
    def create_traffic_influence_subscription(self, target_dnai: str) -> bool:
        """
        Create or update a Traffic Influence subscription via NEF
        
        Args:
            target_dnai: The DNAI to steer traffic to (edge1 or edge2)
        
        Returns:
            True if successful, False otherwise
        """
        url = f"{self.nef_url}/3gpp-traffic-influence/v1/{self.af_id}/subscriptions"
        
        payload = {
            "afServiceId": "dynamic-steering",
            "afAppId": "traffic-steering-agent",
            "dnn": DNN,
            "snssai": {
                "sst": SST,
                "sd": SD
            },
            "anyUeInd": True,  # Apply to all UEs
            "trafficFilters": [
                {
                    "flowId": 1,
                    "flowDescriptions": [
                        "permit out ip from any to any"  # All traffic
                    ]
                }
            ],
            "trafficRoutes": [
                {
                    "dnai": target_dnai
                }
            ]
        }
        
        try:
            if self.subscription_id:
                # Update existing subscription
                url = f"{url}/{self.subscription_id}"
                resp = requests.put(url, json=payload, timeout=10)
            else:
                # Create new subscription
                resp = requests.post(url, json=payload, timeout=10)
            
            if resp.status_code in [200, 201]:
                data = resp.json()
                # Extract subscription ID from self link
                self_link = data.get("self", "")
                if self_link:
                    self.subscription_id = self_link.split("/")[-1]
                
                logger.info(f"✓ Traffic steering subscription created/updated: {target_dnai}")
                logger.info(f"  Subscription ID: {self.subscription_id}")
                return True
            else:
                logger.error(f"NEF API error {resp.status_code}: {resp.text}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to call NEF API: {e}")
            return False
    
    def decide_steering(self, upf1_metrics: UPFMetrics, upf2_metrics: UPFMetrics) -> Optional[str]:
        """
        Make a steering decision based on UPF metrics
        
        Args:
            upf1_metrics: Metrics for AnchorUPF1 (edge1)
            upf2_metrics: Metrics for AnchorUPF2 (edge2)
        
        Returns:
            Target DNAI if steering needed, None otherwise
        """
        upf1_load = upf1_metrics.total_rate_mbps
        upf2_load = upf2_metrics.total_rate_mbps
        
        logger.info(f"📊 UPF Metrics:")
        logger.info(f"   AnchorUPF1 (edge1): {upf1_load:.2f} Mbps")
        logger.info(f"   AnchorUPF2 (edge2): {upf2_load:.2f} Mbps")
        logger.info(f"   Current routing: {self.current_dnai}")
        logger.info(f"   Threshold: {THRESHOLD_MBPS} Mbps")
        
        # If current UPF is overloaded and other is not, switch
        if self.current_dnai == EDGE1_DNAI:
            if upf1_load > THRESHOLD_MBPS and upf2_load < THRESHOLD_MBPS:
                logger.info(f"⚠️  edge1 overloaded ({upf1_load:.2f} > {THRESHOLD_MBPS}), switching to edge2")
                return EDGE2_DNAI
        else:  # current is edge2
            if upf2_load > THRESHOLD_MBPS and upf1_load < THRESHOLD_MBPS:
                logger.info(f"⚠️  edge2 overloaded ({upf2_load:.2f} > {THRESHOLD_MBPS}), switching to edge1")
                return EDGE1_DNAI
        
        # Load balancing: if both are under threshold, prefer the less loaded one
        if upf1_load < THRESHOLD_MBPS and upf2_load < THRESHOLD_MBPS:
            # Only switch if difference is significant (>0.5 Mbps)
            if self.current_dnai == EDGE1_DNAI and (upf1_load - upf2_load) > 0.5:
                logger.info(f"📈 Load balancing: edge2 is less loaded, considering switch")
                # Don't switch too frequently - keep current for stability
                pass
            elif self.current_dnai == EDGE2_DNAI and (upf2_load - upf1_load) > 0.5:
                logger.info(f"📈 Load balancing: edge1 is less loaded, considering switch")
                pass
        
        return None  # No steering needed
    
    def run(self):
        """Main agent loop"""
        logger.info("=" * 60)
        logger.info("🚀 Dynamic Traffic Steering Agent Started")
        logger.info("=" * 60)
        logger.info(f"   Prometheus: {self.prometheus_url}")
        logger.info(f"   NEF: {self.nef_url}")
        logger.info(f"   AF ID: {self.af_id}")
        logger.info(f"   Poll Interval: {POLL_INTERVAL}s")
        logger.info(f"   Threshold: {THRESHOLD_MBPS} Mbps")
        logger.info("=" * 60)
        
        while True:
            try:
                # Collect metrics
                upf1_metrics = self.get_upf_metrics(UPF1_POD_PATTERN, "n6")
                upf2_metrics = self.get_upf_metrics(UPF2_POD_PATTERN, "n6")
                
                if upf1_metrics is None or upf2_metrics is None:
                    logger.warning("Failed to get UPF metrics, retrying...")
                    time.sleep(POLL_INTERVAL)
                    continue
                
                # Make steering decision
                target_dnai = self.decide_steering(upf1_metrics, upf2_metrics)
                
                if target_dnai and target_dnai != self.current_dnai:
                    logger.info(f"🔄 Steering traffic from {self.current_dnai} to {target_dnai}")
                    if self.create_traffic_influence_subscription(target_dnai):
                        self.current_dnai = target_dnai
                        logger.info(f"✅ Traffic now routed through {target_dnai}")
                    else:
                        logger.error("Failed to apply steering decision")
                else:
                    logger.info(f"✓ No steering needed, current: {self.current_dnai}")
                
                print()  # Blank line between iterations
                
            except KeyboardInterrupt:
                logger.info("Shutting down agent...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
            
            time.sleep(POLL_INTERVAL)


def main():
    """Entry point"""
    agent = TrafficSteeringAgent()
    agent.run()


if __name__ == "__main__":
    main()
