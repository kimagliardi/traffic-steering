# Traffic Steering Lab Guide

## Overview

This lab demonstrates **ULCL (Uplink Classifier) Traffic Steering** in a 5G network using free5GC. Traffic is steered based on destination IP:

- **Traffic to MEC App (10.0.2.105)** → Routes through **UPFb → AnchorUPF2** (edge/MEC path)
- **Traffic to Internet (any other IP)** → Routes through **UPFb → N6** directly (internet path)

## Architecture

```
                                    ┌─────────────┐
                                    │  Internet   │
                                    └──────▲──────┘
                                           │ N6
┌──────┐    ┌──────┐    ┌──────────────────┴───────────────────┐
│  UE  │───►│ gNB  │───►│              UPFb                    │
│      │ N3 │      │    │         (BranchingUPF1)              │
└──────┘    └──────┘    │     PFCP: 10.100.50.241              │
                        └──────────────┬───────────────────────┘
                                       │ N9
                        ┌──────────────┴───────────────────────┐
                        │                                       │
                 ┌──────▼──────┐                        ┌──────▼──────┐
                 │  AnchorUPF1 │                        │  AnchorUPF2 │
                 │   (upf1)    │                        │   (upf2)    │
                 │ Internet GW │                        │   MEC GW    │
                 └──────┬──────┘                        └──────┬──────┘
                        │ N6                                   │ N6
                 ┌──────▼──────┐                        ┌──────▼──────┐
                 │  Internet   │                        │  MEC App    │
                 │  (8.8.8.8)  │                        │ 10.0.2.105  │
                 └─────────────┘                        └─────────────┘
```

## Traffic Steering Rules (from SMF uerouting.yaml)

```yaml
UE1:
  members:
    - imsi-208930000000001
  topology:
    - A: gNB1
      B: BranchingUPF1
    - A: BranchingUPF1
      B: AnchorUPF1
    - A: BranchingUPF1
      B: AnchorUPF2
  specificPath:
    - dest: 10.0.2.105/32  # MEC app - route through AnchorUPF2
      path: [BranchingUPF1, AnchorUPF2]
```

---

## Lab Setup

### Prerequisites
- VMs running: `ns` (K8s master), `ns2` (worker), `vm3` (UERANSIM)
- free5GC deployed with ULCL topology
- UE registered and PDU session established

### 1. Verify Components

```bash
# On ns VM - Check all UPFs are running
microk8s kubectl get pods -n free5gc | grep upf

# Expected output:
# free5gc-v1-free5gc-upf-upf1-xxx    1/1     Running
# free5gc-v1-free5gc-upf-upf2-xxx    1/1     Running
# free5gc-v1-free5gc-upf-upfb-xxx    1/1     Running
# free5gc-v1-free5gc-upf-upfb2-xxx   1/1     Running
```

### 2. Check MEC App

```bash
# Get MEC App IP
microk8s kubectl get pod -n free5gc -l app=oai-mec-app-1 -o wide

# Verify MEC App is responding
microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l app=oai-mec-app-1 -o jsonpath='{.items[0].metadata.name}') -- curl -s localhost:80/
```

### 3. Start UE on vm3

```bash
# SSH to vm3
vagrant ssh vm3

# Start gNB (if not running)
cd ~/UERANSIM
sudo ./build/nr-gnb -c config/free5gc-gnb.yaml &

# Start UE
sudo ./build/nr-ue -c config/free5gc-ue.yaml &

# Verify UE tunnel interface
ip addr show uesimtun0

# Expected: UE IP like 10.1.0.1/32
```

---

## Lab Exercises

### Exercise 1: Baseline Traffic (Internet Path)

**Objective:** Verify traffic to internet goes through UPFb N6 directly

#### Step 1: Record baseline counters
```bash
# On ns VM - Record UPF counters before test
echo "=== BEFORE TEST ===" && \
for upf in upf1 upf2 upfb upfb2; do
  echo "--- $upf ---"
  microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l nf=$upf -o jsonpath='{.items[0].metadata.name}') -- cat /proc/net/dev 2>/dev/null | grep -E "n3|n6|n9"
done
```

#### Step 2: Generate internet traffic from UE
```bash
# On vm3 - Ping external IP through UE tunnel
ping -I uesimtun0 -c 10 8.8.8.8

# Or use iperf to an internet server (if available)
# iperf3 -c <internet-iperf-server> -B <ue-ip> -t 10
```

#### Step 3: Check counters after test
```bash
# On ns VM - Record UPF counters after test
echo "=== AFTER TEST ===" && \
for upf in upf1 upf2 upfb upfb2; do
  echo "--- $upf ---"
  microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l nf=$upf -o jsonpath='{.items[0].metadata.name}') -- cat /proc/net/dev 2>/dev/null | grep -E "n3|n6|n9"
done
```

#### Expected Result:
- **UPFb N3**: RX bytes increased (traffic from gNB)
- **UPFb N6**: TX bytes increased (traffic to internet)
- **UPF1/UPF2 N9**: Minimal/no change (not used for this traffic)

---

### Exercise 2: MEC Traffic Steering

**Objective:** Verify traffic to MEC App (10.0.2.105) goes through AnchorUPF2

#### Step 1: Start iperf server on MEC App
```bash
# On ns VM - Install and start iperf3 on MEC App
microk8s kubectl exec -it -n free5gc $(microk8s kubectl get pods -n free5gc -l app=oai-mec-app-1 -o jsonpath='{.items[0].metadata.name}') -- bash -c "apt-get update && apt-get install -y iperf3 && iperf3 -s -D"

# Get MEC App IP (should be 10.0.2.105 or pod IP)
MEC_IP=$(microk8s kubectl get pod -n free5gc -l app=oai-mec-app-1 -o jsonpath='{.items[0].status.podIP}')
echo "MEC App IP: $MEC_IP"
```

#### Step 2: Record baseline counters
```bash
# On ns VM
echo "=== BEFORE MEC TEST ===" && \
for upf in upf1 upf2 upfb upfb2; do
  echo "--- $upf ---"
  microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l nf=$upf -o jsonpath='{.items[0].metadata.name}') -- cat /proc/net/dev 2>/dev/null | grep -E "n3|n6|n9"
done
```

#### Step 3: Run iperf from UE to MEC App
```bash
# On vm3 - Run iperf3 client through UE tunnel
# First, install iperf3 if needed
sudo apt-get install -y iperf3

# Run iperf to MEC App (use the configured steering IP)
iperf3 -c 10.0.2.105 -B $(ip addr show uesimtun0 | grep inet | awk '{print $2}' | cut -d/ -f1) -t 10

# Alternative: If MEC app has different IP
# iperf3 -c $MEC_IP -B <ue-ip> -t 10
```

#### Step 4: Check counters after test
```bash
# On ns VM
echo "=== AFTER MEC TEST ===" && \
for upf in upf1 upf2 upfb upfb2; do
  echo "--- $upf ---"
  microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l nf=$upf -o jsonpath='{.items[0].metadata.name}') -- cat /proc/net/dev 2>/dev/null | grep -E "n3|n6|n9"
done
```

#### Expected Result:
- **UPFb N3**: RX bytes increased (traffic from gNB)
- **UPFb N9**: TX bytes increased (traffic to AnchorUPF2)
- **AnchorUPF2 (upf2) N9**: RX bytes increased (traffic from UPFb)
- **AnchorUPF2 (upf2) N6**: TX bytes increased (traffic to MEC App)
- **AnchorUPF1 (upf1)**: Minimal/no change

---

### Exercise 3: Side-by-Side Comparison

**Objective:** Run both traffic types and compare paths

#### Step 1: Clear counters (restart pods)
```bash
# On ns VM - Restart UPF pods to reset counters
microk8s kubectl rollout restart deployment -n free5gc -l app.kubernetes.io/component=upf
sleep 30
```

#### Step 2: Generate INTERNET traffic
```bash
# On vm3
ping -I uesimtun0 -c 100 8.8.8.8
```

#### Step 3: Record "Internet Only" counters
```bash
# On ns VM
echo "=== INTERNET TRAFFIC ONLY ===" && \
for upf in upf1 upf2 upfb; do
  echo "--- $upf ---"
  microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l nf=$upf -o jsonpath='{.items[0].metadata.name}') -- cat /proc/net/dev 2>/dev/null | grep -E "n3|n6|n9" | awk '{print $1, "RX:"$2, "TX:"$10}'
done
```

#### Step 4: Generate MEC traffic
```bash
# On vm3
iperf3 -c 10.0.2.105 -B $(ip addr show uesimtun0 | grep inet | awk '{print $2}' | cut -d/ -f1) -t 10
```

#### Step 5: Record "After MEC" counters
```bash
# On ns VM
echo "=== AFTER MEC TRAFFIC ===" && \
for upf in upf1 upf2 upfb; do
  echo "--- $upf ---"
  microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l nf=$upf -o jsonpath='{.items[0].metadata.name}') -- cat /proc/net/dev 2>/dev/null | grep -E "n3|n6|n9" | awk '{print $1, "RX:"$2, "TX:"$10}'
done
```

---

## Monitoring with Grafana

### Access Grafana Dashboard
```
URL: http://192.168.56.119:30030
Username: admin
Password: admin
```

### Import UPF Traffic Dashboard
1. Go to Dashboards → Import
2. Paste the JSON from `grafana-upf-traffic-dashboard.json`
3. Select Prometheus datasource

### Key Metrics to Watch
- **UPF Egress Traffic**: Shows TX bytes per UPF interface
- **UPF Ingress Traffic**: Shows RX bytes per UPF interface  
- **Traffic Distribution**: Pie chart showing UPF1 vs UPF2 traffic
- **Packet Rate**: Bar chart comparing packet rates

---

## Troubleshooting

### UE Can't Reach MEC App
```bash
# Check if PDU session is established
microk8s kubectl logs deployment/free5gc-v1-free5gc-smf-smf -n free5gc | grep -i "pdu session"

# Check UPF FAR/PDR rules
microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l nf=upfb -o jsonpath='{.items[0].metadata.name}') -- cat /proc/gtp5g/far
```

### Traffic Not Being Steered
```bash
# Check SMF routing config
microk8s kubectl get configmap free5gc-v1-free5gc-smf-configmap -n free5gc -o jsonpath='{.data.uerouting\.yaml}'

# Check traffic influence policies in MongoDB
microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l app.kubernetes.io/name=mongodb -o jsonpath='{.items[0].metadata.name}') -- mongosh --quiet --eval 'db = db.getSiblingDB("free5gc"); db.getCollection("applicationData.influenceData").find().toArray()'
```

### No Metrics in Prometheus
```bash
# Check Prometheus is scraping
curl -s http://192.168.56.119:30090/api/v1/targets | grep -i upf

# Check cAdvisor metrics
curl -s http://192.168.56.119:30090/api/v1/query?query=container_network_transmit_bytes_total
```

---

## Summary Table

| Traffic Type | Source | Destination | Path |
|-------------|--------|-------------|------|
| Internet | UE (10.1.0.x) | 8.8.8.8 | UE → gNB → UPFb(N3) → UPFb(N6) → Internet |
| MEC | UE (10.1.0.x) | 10.0.2.105 | UE → gNB → UPFb(N3) → UPFb(N9) → UPF2(N9) → UPF2(N6) → MEC |

---

## Quick Reference Commands

```bash
# Check all UPF interfaces at once
for upf in upf1 upf2 upfb upfb2; do echo "=== $upf ===" && microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l nf=$upf -o jsonpath='{.items[0].metadata.name}') -- cat /proc/net/dev 2>/dev/null | head -10; done

# Watch traffic in real-time (run every 2 seconds)
watch -n 2 'for upf in upfb upf2; do echo "=== $upf ===" && microk8s kubectl exec -n free5gc $(microk8s kubectl get pods -n free5gc -l nf=$upf -o jsonpath='{.items[0].metadata.name}') -- cat /proc/net/dev 2>/dev/null | grep -E "n6|n9"; done'

# Check UE IP
vagrant ssh vm3 -c 'ip addr show uesimtun0'

# Grafana URL
echo "Grafana: http://192.168.56.119:30030 (admin/admin)"

# Prometheus URL
echo "Prometheus: http://192.168.56.119:30090"
```
