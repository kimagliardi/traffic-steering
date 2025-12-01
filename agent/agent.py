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
import logging
from typing import Optional

from tools import (
    AgentConfig,
    TrafficSteeringToolkit,
    SteeringTarget,
    UPFMetrics,
    OperationResult
)

# Configuration from environment
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))  # seconds
THRESHOLD_MBPS = float(os.getenv("THRESHOLD_MBPS", "1.0"))  # threshold for steering
HYSTERESIS_MBPS = float(os.getenv("HYSTERESIS_MBPS", "0.3"))  # prevent flapping

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TrafficSteeringAgent:
    """Agent that monitors UPF metrics and steers traffic dynamically"""
    
    def __init__(self, config: AgentConfig = None):
        self.config = config or AgentConfig()
        self.toolkit = TrafficSteeringToolkit(self.config)
        
        # Steering state
        self.current_target: Optional[SteeringTarget] = None
        self.last_steering_time: float = 0
        self.min_steering_interval: float = 30.0  # Minimum seconds between steering decisions
        
        # Metrics history for smoothing
        self.upf1_history: list = []
        self.upf2_history: list = []
        self.history_size: int = 3
    
    def get_smoothed_metrics(self) -> tuple[Optional[UPFMetrics], Optional[UPFMetrics]]:
        """Get metrics with smoothing to prevent flapping"""
        upf1_result = self.toolkit.prometheus.get_upf1_metrics()
        upf2_result = self.toolkit.prometheus.get_upf2_metrics()
        
        upf1 = upf1_result.data if upf1_result.success else None
        upf2 = upf2_result.data if upf2_result.success else None
        
        # Add to history
        if upf1:
            self.upf1_history.append(upf1.total_rate_mbps)
            if len(self.upf1_history) > self.history_size:
                self.upf1_history.pop(0)
        
        if upf2:
            self.upf2_history.append(upf2.total_rate_mbps)
            if len(self.upf2_history) > self.history_size:
                self.upf2_history.pop(0)
        
        # Return smoothed metrics
        if upf1 and self.upf1_history:
            upf1.tx_rate_mbps = sum(self.upf1_history) / len(self.upf1_history) / 2
            upf1.rx_rate_mbps = upf1.tx_rate_mbps
        
        if upf2 and self.upf2_history:
            upf2.tx_rate_mbps = sum(self.upf2_history) / len(self.upf2_history) / 2
            upf2.rx_rate_mbps = upf2.tx_rate_mbps
        
        return upf1, upf2
    
    def decide_steering(self, upf1: UPFMetrics, upf2: UPFMetrics) -> Optional[SteeringTarget]:
        """
        Make a steering decision based on UPF metrics.
        
        Returns target DNAI if steering is needed, None otherwise.
        Uses hysteresis to prevent flapping.
        """
        upf1_load = upf1.total_rate_mbps
        upf2_load = upf2.total_rate_mbps
        
        logger.info(f"📊 UPF Metrics:")
        logger.info(f"   AnchorUPF1 (edge1): {upf1_load:.2f} Mbps")
        logger.info(f"   AnchorUPF2 (edge2): {upf2_load:.2f} Mbps")
        logger.info(f"   Current routing: {self.current_target.value if self.current_target else 'None'}")
        logger.info(f"   Threshold: {THRESHOLD_MBPS} Mbps (hysteresis: {HYSTERESIS_MBPS})")
        
        # Check minimum steering interval
        time_since_last = time.time() - self.last_steering_time
        if time_since_last < self.min_steering_interval:
            logger.info(f"   ⏳ Cooldown: {self.min_steering_interval - time_since_last:.0f}s remaining")
            return None
        
        # Initial steering - no current target
        if self.current_target is None:
            if upf1_load <= upf2_load:
                return SteeringTarget.EDGE1
            else:
                return SteeringTarget.EDGE2
        
        # Steering logic with hysteresis
        if self.current_target == SteeringTarget.EDGE1:
            # Currently on edge1, consider switching to edge2
            if upf1_load > THRESHOLD_MBPS + HYSTERESIS_MBPS and upf2_load < THRESHOLD_MBPS - HYSTERESIS_MBPS:
                logger.info(f"⚠️  edge1 overloaded ({upf1_load:.2f} > {THRESHOLD_MBPS + HYSTERESIS_MBPS})")
                return SteeringTarget.EDGE2
        else:
            # Currently on edge2, consider switching to edge1
            if upf2_load > THRESHOLD_MBPS + HYSTERESIS_MBPS and upf1_load < THRESHOLD_MBPS - HYSTERESIS_MBPS:
                logger.info(f"⚠️  edge2 overloaded ({upf2_load:.2f} > {THRESHOLD_MBPS + HYSTERESIS_MBPS})")
                return SteeringTarget.EDGE1
        
        return None
    
    def apply_steering(self, target: SteeringTarget) -> bool:
        """Apply steering decision"""
        logger.info(f"🔄 Steering traffic to {target.value}...")
        
        result = self.toolkit.steer_traffic(target)
        
        if result.success:
            self.current_target = target
            self.last_steering_time = time.time()
            logger.info(f"✅ {result.message}")
            return True
        else:
            logger.error(f"❌ Steering failed: {result.message}")
            if result.error:
                logger.error(f"   Error: {result.error}")
            return False
    
    def initialize(self) -> bool:
        """Initialize agent - ensure system is ready"""
        logger.info("🔧 Initializing agent...")
        
        # Run health check
        health_result = self.toolkit.health.run_full_health_check()
        
        if not health_result.success:
            logger.warning("⚠️  System not fully healthy, attempting recovery...")
            
            recovery_result = self.toolkit.ensure_system_ready()
            if not recovery_result.success:
                logger.error("❌ System recovery failed")
                return False
        
        # Get current state
        state_result = self.toolkit.get_current_steering_state()
        if state_result.success and state_result.data:
            current_dnai = state_result.data.get("subscription_dnai")
            if current_dnai == "edge1":
                self.current_target = SteeringTarget.EDGE1
            elif current_dnai == "edge2":
                self.current_target = SteeringTarget.EDGE2
            
            logger.info(f"📍 Current steering target: {self.current_target}")
        
        logger.info("✅ Agent initialized")
        return True
    
    def run(self):
        """Main agent loop"""
        logger.info("=" * 60)
        logger.info("🚀 Dynamic Traffic Steering Agent Started")
        logger.info("=" * 60)
        logger.info(f"   Prometheus: {self.config.prometheus_url}")
        logger.info(f"   NEF: {self.config.nef_url}")
        logger.info(f"   AF ID: {self.config.af_id}")
        logger.info(f"   Poll Interval: {POLL_INTERVAL}s")
        logger.info(f"   Threshold: {THRESHOLD_MBPS} Mbps")
        logger.info(f"   Hysteresis: {HYSTERESIS_MBPS} Mbps")
        logger.info("=" * 60)
        
        # Initialize
        if not self.initialize():
            logger.error("Initialization failed, exiting")
            return
        
        # Main loop
        while True:
            try:
                # Get metrics
                upf1, upf2 = self.get_smoothed_metrics()
                
                if upf1 is None or upf2 is None:
                    logger.warning("Failed to get UPF metrics, checking system health...")
                    health_result = self.toolkit.health.check_upf_health()
                    if not health_result.success:
                        logger.warning("UPF health issues detected, attempting recovery...")
                        self.toolkit.ensure_system_ready()
                    time.sleep(POLL_INTERVAL)
                    continue
                
                # Make steering decision
                target = self.decide_steering(upf1, upf2)
                
                if target and target != self.current_target:
                    self.apply_steering(target)
                else:
                    current = self.current_target.value if self.current_target else "None"
                    logger.info(f"✓ No steering needed, current: {current}")
                
                print()  # Blank line between iterations
                
            except KeyboardInterrupt:
                logger.info("Shutting down agent...")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                import traceback
                traceback.print_exc()
            
            time.sleep(POLL_INTERVAL)


def main():
    """Entry point"""
    config = AgentConfig()
    agent = TrafficSteeringAgent(config)
    agent.run()


if __name__ == "__main__":
    main()
