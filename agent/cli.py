#!/usr/bin/env python3
"""
Traffic Steering CLI Tool
=========================
Command-line interface for manual traffic steering operations.

Usage:
    python cli.py status              - Show system status
    python cli.py steer edge1         - Steer traffic to edge1
    python cli.py steer edge2         - Steer traffic to edge2
    python cli.py subscriptions       - List all subscriptions
    python cli.py cleanup             - Clean up stale subscriptions
    python cli.py restart ue          - Restart the UE
    python cli.py restart smf         - Restart SMF
    python cli.py restart upf         - Restart all UPFs
    python cli.py test                - Run a quick connectivity test
    python cli.py health              - Run health check
    python cli.py metrics             - Show current metrics
"""

import argparse
import sys
import json

from tools import (
    AgentConfig,
    TrafficSteeringToolkit,
    SteeringTarget,
    OperationResult
)


def print_result(result: OperationResult, verbose: bool = False):
    """Pretty print an operation result"""
    if result.success:
        print(f"✅ {result.message}")
    else:
        print(f"❌ {result.message}")
        if result.error:
            print(f"   Error: {result.error}")
    
    if verbose and result.data:
        print(f"\n📋 Data:")
        if isinstance(result.data, dict):
            print(json.dumps(result.data, indent=2, default=str))
        else:
            print(result.data)


def cmd_status(toolkit: TrafficSteeringToolkit, args):
    """Show system status"""
    print("=" * 60)
    print("📊 System Status")
    print("=" * 60)
    
    # Get current steering state
    state = toolkit.get_current_steering_state()
    
    if state.success and state.data:
        data = state.data
        print(f"\n🎯 Current Steering Target:")
        print(f"   DNAI: {data.get('subscription_dnai') or 'None'}")
        print(f"   UE IP: {data.get('ue_ip') or 'Unknown'}")
        print(f"   Connected UPF: {data.get('connected_upf') or 'Unknown'}")
        
        subs = data.get("subscriptions", [])
        print(f"   Active Subscriptions: {len(subs)}")
    else:
        print(f"⚠️  Could not get steering state: {state.message}")
    
    # Health check
    print(f"\n🏥 Health Check:")
    health = toolkit.health.run_full_health_check()
    
    if health.data:
        for check_name, check_result in health.data.items():
            icon = "✓" if check_result.get("healthy") else "✗"
            print(f"   {icon} {check_name}: {check_result.get('message', '')}")
        
        print(f"\n   Overall: {'Healthy' if health.success else 'Issues Detected'}")
    
    return 0 if state.success else 1


def cmd_steer(toolkit: TrafficSteeringToolkit, args):
    """Steer traffic to a target"""
    target_str = args.target.lower()
    
    if target_str == "edge1":
        target = SteeringTarget.EDGE1
    elif target_str == "edge2":
        target = SteeringTarget.EDGE2
    else:
        print(f"❌ Invalid target: {target_str}")
        print("   Valid targets: edge1, edge2")
        return 1
    
    print(f"🔄 Steering traffic to {target.value}...")
    
    result = toolkit.steer_traffic(target)
    print_result(result, args.verbose)
    
    return 0 if result.success else 1


def cmd_subscriptions(toolkit: TrafficSteeringToolkit, args):
    """List subscriptions"""
    result = toolkit.nef.list_subscriptions()
    
    if not result.success:
        print_result(result)
        return 1
    
    subs = result.data or []
    
    if not subs:
        print("📋 No subscriptions found")
        return 0
    
    print(f"📋 Subscriptions ({len(subs)} total):")
    print("-" * 60)
    
    for sub in subs:
        print(f"  📄 Subscription: {sub.subscription_id}")
        print(f"     DNAI: {sub.dnai}")
        print(f"     S-NSSAI: sst={sub.snssai.get('sst', 'N/A')}, sd={sub.snssai.get('sd', 'N/A')}")
        print(f"     DNN: {sub.dnn}")
        print(f"     Any UE: {sub.any_ue_ind}")
        print()
    
    return 0


def cmd_cleanup(toolkit: TrafficSteeringToolkit, args):
    """Clean up stale subscriptions"""
    print("🧹 Cleaning up stale subscriptions...")
    
    result = toolkit.nef.delete_all_subscriptions()
    print_result(result, args.verbose)
    
    return 0 if result.success else 1


def cmd_restart(toolkit: TrafficSteeringToolkit, args):
    """Restart a component"""
    component = args.component.lower()
    
    if component == "ue":
        print("🔄 Restarting UE...")
        result = toolkit.ue.restart_ue()
    elif component == "smf":
        print("🔄 Restarting SMF...")
        result = toolkit.k8s.restart_smf()
    elif component == "upf":
        print("🔄 Restarting all UPFs...")
        result = toolkit.k8s.restart_all_upfs()
    elif component == "all":
        print("🔄 Full system recovery...")
        result = toolkit.ensure_system_ready()
    else:
        print(f"❌ Unknown component: {component}")
        print("   Valid components: ue, smf, upf, all")
        return 1
    
    print_result(result, args.verbose)
    return 0 if result.success else 1


def cmd_test(toolkit: TrafficSteeringToolkit, args):
    """Run connectivity test"""
    print("🧪 Running connectivity test...")
    
    # Get current UE IP
    ip_result = toolkit.ue.get_ue_ip()
    if not ip_result.success:
        print(f"❌ Could not get UE IP: {ip_result.message}")
        return 1
    
    ue_ip = ip_result.data.get("ip", "")
    upf = ip_result.data.get("upf", "unknown")
    print(f"   UE IP: {ue_ip}")
    print(f"   Connected to: {upf}")
    
    # Determine expected pool
    if ue_ip.startswith("10.1.0."):
        print(f"   Pool: edge1 (10.1.0.0/17)")
    elif ue_ip.startswith("10.1.128."):
        print(f"   Pool: edge2 (10.1.128.0/17)")
    else:
        print(f"   Pool: Unknown")
    
    # Test connectivity
    print("\n   Testing internet connectivity...")
    conn_result = toolkit.ue.ping_test()
    
    if conn_result.success:
        print(f"   ✅ Connectivity OK")
    else:
        print(f"   ❌ Connectivity failed")
    
    return 0 if conn_result.success else 1


def cmd_health(toolkit: TrafficSteeringToolkit, args):
    """Run health check"""
    print("🏥 Running health check...")
    print("=" * 60)
    
    result = toolkit.health.run_full_health_check()
    
    if result.data:
        for check_name, check_result in result.data.items():
            icon = "✅" if check_result.get("healthy") else "❌"
            print(f"{icon} {check_name}")
            print(f"     {check_result.get('message', '')}")
            print()
    
    print("-" * 60)
    if result.success:
        print("✅ All checks passed")
        return 0
    else:
        print("⚠️  Some checks failed")
        return 1


def cmd_metrics(toolkit: TrafficSteeringToolkit, args):
    """Show current metrics"""
    print("📊 Current Metrics")
    print("=" * 60)
    
    # UPF1 metrics
    upf1 = toolkit.prometheus.get_upf1_metrics()
    if upf1.success and upf1.data:
        m = upf1.data
        print(f"\n📈 AnchorUPF1 (edge1):")
        print(f"   TX Rate: {m.tx_rate_mbps:.2f} Mbps")
        print(f"   RX Rate: {m.rx_rate_mbps:.2f} Mbps")
        print(f"   Total:   {m.total_rate_mbps:.2f} Mbps")
    else:
        print(f"\n⚠️  AnchorUPF1 metrics unavailable")
    
    # UPF2 metrics
    upf2 = toolkit.prometheus.get_upf2_metrics()
    if upf2.success and upf2.data:
        m = upf2.data
        print(f"\n📈 AnchorUPF2 (edge2):")
        print(f"   TX Rate: {m.tx_rate_mbps:.2f} Mbps")
        print(f"   RX Rate: {m.rx_rate_mbps:.2f} Mbps")
        print(f"   Total:   {m.total_rate_mbps:.2f} Mbps")
    else:
        print(f"\n⚠️  AnchorUPF2 metrics unavailable")
    
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Traffic Steering CLI Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # status command
    subparsers.add_parser("status", help="Show system status")
    
    # steer command
    steer_parser = subparsers.add_parser("steer", help="Steer traffic to a target")
    steer_parser.add_argument("target", choices=["edge1", "edge2"], help="Target DNAI")
    
    # subscriptions command
    subparsers.add_parser("subscriptions", aliases=["subs"], help="List subscriptions")
    
    # cleanup command
    subparsers.add_parser("cleanup", help="Clean up stale subscriptions")
    
    # restart command
    restart_parser = subparsers.add_parser("restart", help="Restart a component")
    restart_parser.add_argument("component", choices=["ue", "smf", "upf", "all"], help="Component to restart")
    
    # test command
    subparsers.add_parser("test", help="Run connectivity test")
    
    # health command
    subparsers.add_parser("health", help="Run health check")
    
    # metrics command
    subparsers.add_parser("metrics", help="Show current metrics")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Initialize toolkit
    config = AgentConfig()
    toolkit = TrafficSteeringToolkit(config)
    
    # Run command
    commands = {
        "status": cmd_status,
        "steer": cmd_steer,
        "subscriptions": cmd_subscriptions,
        "subs": cmd_subscriptions,
        "cleanup": cmd_cleanup,
        "restart": cmd_restart,
        "test": cmd_test,
        "health": cmd_health,
        "metrics": cmd_metrics,
    }
    
    cmd_func = commands.get(args.command)
    if cmd_func:
        return cmd_func(toolkit, args)
    else:
        print(f"Unknown command: {args.command}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
