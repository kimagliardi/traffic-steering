# Free5GC ULCL Traffic Steering Guide

## Overview

This document describes how to configure and test DNAI-based traffic steering in free5GC using the ULCL (Uplink Classifier) architecture. Traffic steering allows dynamic routing of UE traffic to different anchor UPFs based on traffic influence subscriptions created via the NEF (Network Exposure Function) API.

## Architecture

```
                    ┌─────────────────┐
                    │      gNB        │
                    │  (UERANSIM)     │
                    └────────┬────────┘
                             │ N3 (192.168.56.19)
                             ▼
                    ┌─────────────────┐
                    │  BranchingUPF1  │  (upfb)
                    │   PFCP: 10.100.50.241
                    │   N9: 10.100.50.225
                    └────────┬────────┘
                             │ N9
              ┌──────────────┴──────────────┐
              ▼                              ▼
    ┌─────────────────┐            ┌─────────────────┐
    │   AnchorUPF1    │            │   AnchorUPF2    │
    │   PFCP: 10.100.50.243        │   PFCP: 10.100.50.245
    │   N9: 10.100.50.227          │   N9: 10.100.50.228
    │   Pool: 10.1.0.0/17          │   Pool: 10.1.128.0/17
    │   DNAI: edge1   │            │   DNAI: edge2   │
    └─────────────────┘            └─────────────────┘
```

### Key Components

| Component | IP Address | Role |
|-----------|------------|------|
| SMF | 10.100.50.244 (PFCP) | Session management, UPF selection |
| NEF | ClusterIP:80 | Traffic influence API |
| BranchingUPF1 (upfb) | 10.100.50.241 | Branching point for ULCL |
| AnchorUPF1 (upf1) | 10.100.50.243 | Anchor UPF with DNAI "edge1" |
| AnchorUPF2 (upf2) | 10.100.50.245 | Anchor UPF with DNAI "edge2" |

### IP Pool Mapping

- **edge1 (AnchorUPF1)**: 10.1.0.0/17 → UE gets IP like 10.1.0.x
- **edge2 (AnchorUPF2)**: 10.1.128.0/17 → UE gets IP like 10.1.128.x

---

## Prerequisites

### 1. Verify Cluster Status

```bash
# SSH to master node
cd /path/to/vagrant
vagrant ssh ns

# Check all pods are running
microk8s kubectl get pods -n free5gc
```

### 2. Verify UPF Associations

All UPFs must be associated with SMF via PFCP:

```bash
# Check SMF logs for PFCP associations
microk8s kubectl logs deploy/free5gc-v1-free5gc-smf-smf -n free5gc | grep -E "PFCP.*Association"
```

Expected output should show associations with:
- 10.100.50.241 (BranchingUPF1)
- 10.100.50.243 (AnchorUPF1)
- 10.100.50.245 (AnchorUPF2)

---

## Configuration

### 1. SMF Configuration (ulcl-enabled-values.yaml)

The critical DNAI configuration is in the `dnnUpfInfoList` section. **The correct format uses `dnaiList` as an array**:

```yaml
AnchorUPF1:
  type: UPF
  nodeID: 10.100.50.243
  sNssaiUpfInfos:
    - sNssai:
        sst: 1
        sd: 010203
      dnnUpfInfoList:
        - dnn: internet
          dnaiList:        # MUST be an array
            - edge1        # DNAI identifier
          pools:
            - cidr: 10.1.0.0/17

AnchorUPF2:
  type: UPF
  nodeID: 10.100.50.245
  sNssaiUpfInfos:
    - sNssai:
        sst: 1
        sd: 010203
      dnnUpfInfoList:
        - dnn: internet
          dnaiList:        # MUST be an array
            - edge2        # DNAI identifier
          pools:
            - cidr: 10.1.128.0/17
```

> ⚠️ **Common Mistake**: Using `dnai: edge1` (single value) instead of `dnaiList: [edge1]` (array) will cause SMF to fail finding UPFs by DNAI.

### 2. UE Routing Configuration

For DNAI-based traffic steering to work properly, the UE routing must include paths to **both** anchor UPFs:

```yaml
ueRoutingInfo:
  UE1:
    members:
      - imsi-208930000000001
    topology:
      - A: gNB1
        B: BranchingUPF1
      - A: BranchingUPF1
        B: AnchorUPF1      # Path to edge1
      - A: BranchingUPF1
        B: AnchorUPF2      # Path to edge2
```

---

## Step-by-Step Testing Procedure

### Step 1: Apply Configuration Changes

```bash
# From your local machine
cd /path/to/traffic-steering/vagrant

# Copy updated values file to VM
cat ../free5gc-helm/charts/free5gc/ulcl-enabled-values.yaml | \
  vagrant ssh ns -c 'cat > ~/free5gc-helm/charts/free5gc/ulcl-enabled-values.yaml'

# Upgrade Helm release
vagrant ssh ns -c 'cd ~/free5gc-helm/charts/free5gc && \
  microk8s helm upgrade free5gc-v1 . -f ulcl-enabled-values.yaml -n free5gc'
```

### Step 2: Restart SMF to Load New Configuration

```bash
# Restart SMF deployment
vagrant ssh ns -c 'microk8s kubectl rollout restart deployment/free5gc-v1-free5gc-smf-smf -n free5gc'

# Wait for rollout
vagrant ssh ns -c 'microk8s kubectl rollout status deployment/free5gc-v1-free5gc-smf-smf -n free5gc --timeout=120s'
```

> ⚠️ **Note**: If the new pod gets stuck in `Init:0/1`, you may need to delete the old pod manually:
> ```bash
> vagrant ssh ns -c 'microk8s kubectl delete pod -n free5gc -l app.kubernetes.io/component=smf --force'
> ```

### Step 3: Verify All UPFs Are Running

```bash
vagrant ssh ns -c 'microk8s kubectl get pods -n free5gc | grep upf'
```

Expected output (all should be `Running`):
```
free5gc-v1-free5gc-upf-upf1-xxx    1/1     Running   0          XXm
free5gc-v1-free5gc-upf-upf2-xxx    1/1     Running   0          XXm
free5gc-v1-free5gc-upf-upfb-xxx    1/1     Running   0          XXm
```

> ⚠️ **If any UPF is in CrashLoopBackOff**, delete the pod to restart it:
> ```bash
> vagrant ssh ns -c 'microk8s kubectl delete pod <pod-name> -n free5gc --force'
> ```

### Step 4: Get NEF Service IP

```bash
vagrant ssh ns -c 'microk8s kubectl get svc -n free5gc | grep nef'
```

Note the ClusterIP (e.g., `10.152.183.162`).

### Step 5: Clean Up Old Traffic Influence Subscriptions

```bash
# List existing subscriptions
vagrant ssh ns -c 'curl -s http://<NEF-IP>:80/3gpp-traffic-influence/v1/test-agent/subscriptions'

# Delete any old subscriptions
vagrant ssh ns -c 'curl -s -X DELETE http://<NEF-IP>:80/3gpp-traffic-influence/v1/test-agent/subscriptions/<ID>'
```

### Step 6: Create Traffic Influence Subscription

#### To route traffic to edge2 (AnchorUPF2):

```bash
vagrant ssh ns -c "curl -s -X POST http://<NEF-IP>:80/3gpp-traffic-influence/v1/test-agent/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{
    \"afServiceId\": \"test\",
    \"afAppId\": \"steering-test\",
    \"dnn\": \"internet\",
    \"snssai\": {\"sst\": 1, \"sd\": \"010203\"},
    \"anyUeInd\": true,
    \"trafficFilters\": [{
      \"flowId\": 1,
      \"flowDescriptions\": [\"permit out ip from any to any\"]
    }],
    \"trafficRoutes\": [{\"dnai\": \"edge2\"}]
  }'"
```

#### To route traffic to edge1 (AnchorUPF1):

```bash
vagrant ssh ns -c "curl -s -X POST http://<NEF-IP>:80/3gpp-traffic-influence/v1/test-agent/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{
    \"afServiceId\": \"test\",
    \"afAppId\": \"steering-test\",
    \"dnn\": \"internet\",
    \"snssai\": {\"sst\": 1, \"sd\": \"010203\"},
    \"anyUeInd\": true,
    \"trafficFilters\": [{
      \"flowId\": 1,
      \"flowDescriptions\": [\"permit out ip from any to any\"]
    }],
    \"trafficRoutes\": [{\"dnai\": \"edge1\"}]
  }'"
```

### Step 7: Start/Restart the UE

```bash
# Kill existing UE process
vagrant ssh vm3 -c "sudo pkill nr-ue"

# Start UE
vagrant ssh vm3 -c "cd ~/ue/UERANSIM && \
  sudo nohup ./build/nr-ue -c config/free5gc-ue.yaml > /tmp/ue.log 2>&1 &"

# Wait for registration
sleep 10

# Check UE logs
vagrant ssh vm3 -c "tail -20 /tmp/ue.log"
```

### Step 8: Verify IP Address Assignment

```bash
vagrant ssh vm3 -c "ip addr show uesimtun0"
```

**Expected results:**
- With `edge1` subscription: IP in range `10.1.0.x/24`
- With `edge2` subscription: IP in range `10.1.128.x/24`

### Step 9: Test Internet Connectivity

```bash
vagrant ssh vm3 -c "ping -I uesimtun0 -c 3 8.8.8.8"
```

---

## API Flow Explanation

### Traffic Influence Subscription Flow

```
┌────┐     ┌─────┐     ┌─────┐     ┌─────┐     ┌─────┐
│ AF │     │ NEF │     │ UDR │     │ PCF │     │ SMF │
└──┬─┘     └──┬──┘     └──┬──┘     └──┬──┘     └──┬──┘
   │          │           │           │           │
   │ POST /subscriptions  │           │           │
   │─────────>│           │           │           │
   │          │           │           │           │
   │          │ PUT influenceData     │           │
   │          │──────────>│           │           │
   │          │           │           │           │
   │          │           │ Notify    │           │
   │          │           │──────────>│           │
   │          │           │           │           │
   │          │           │           │ PCC Rules │
   │          │           │           │──────────>│
   │          │           │           │           │
   │          │           │           │     ┌─────┴─────┐
   │          │           │           │     │ Select UPF │
   │          │           │           │     │ by DNAI    │
   │          │           │           │     └───────────┘
```

1. **AF (Application Function)** creates a traffic influence subscription via NEF API
2. **NEF** stores the subscription data in **UDR** (MongoDB `applicationData.influenceData` collection)
3. **UDR** notifies **PCF** about the traffic influence data
4. **PCF** creates PCC (Policy and Charging Control) rules
5. When UE establishes a PDU session, **SMF** queries for applicable traffic influence
6. **SMF** selects the UPF matching the specified DNAI

### Key API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/3gpp-traffic-influence/v1/{afId}/subscriptions` | GET | List all subscriptions |
| `/3gpp-traffic-influence/v1/{afId}/subscriptions` | POST | Create new subscription |
| `/3gpp-traffic-influence/v1/{afId}/subscriptions/{subscriptionId}` | DELETE | Delete subscription |
| `/3gpp-traffic-influence/v1/{afId}/subscriptions/{subscriptionId}` | PUT | Update subscription |

### Traffic Influence Subscription Schema

```json
{
  "afServiceId": "string",           // AF service identifier
  "afAppId": "string",               // Application identifier
  "dnn": "internet",                 // Data Network Name
  "snssai": {                        // Network slice
    "sst": 1,
    "sd": "010203"
  },
  "anyUeInd": true,                  // Apply to any UE
  "trafficFilters": [{               // Traffic matching rules
    "flowId": 1,
    "flowDescriptions": ["permit out ip from any to any"]
  }],
  "trafficRoutes": [{                // Routing decision
    "dnai": "edge2"                  // Target DNAI
  }]
}
```

---

## Troubleshooting

### Issue: SMF says "Can't find UPF with DNAI[xxx]"

**Cause**: DNAI not configured correctly in SMF config.

**Solution**: Verify `dnaiList` is an array in `ulcl-enabled-values.yaml`:
```yaml
dnnUpfInfoList:
  - dnn: internet
    dnaiList:      # MUST be array format
      - edge1
    pools:
      - cidr: 10.1.0.0/17
```

### Issue: UPF in CrashLoopBackOff

**Cause**: GTP5G kernel module or interface issues.

**Solution**:
```bash
# Delete the crashing pod
vagrant ssh ns -c 'microk8s kubectl delete pod <pod-name> -n free5gc --force'

# If persists, check GTP5G module on worker node
vagrant ssh ns2 -c 'lsmod | grep gtp5g'
```

### Issue: SMF pod stuck in Init:0/1

**Cause**: Init container waiting for NRF or old pod blocking.

**Solution**:
```bash
# Delete old pods
vagrant ssh ns -c 'microk8s kubectl delete pod -n free5gc -l app.kubernetes.io/component=smf --force'

# Wait for new pod
sleep 30
vagrant ssh ns -c 'microk8s kubectl get pods -n free5gc | grep smf'
```

### Issue: UE gets wrong IP pool

**Cause**: Old traffic influence subscription still active, or UPF selection cached.

**Solution**:
```bash
# Clear all subscriptions
vagrant ssh ns -c 'curl -s http://<NEF-IP>:80/3gpp-traffic-influence/v1/test-agent/subscriptions'
# Delete each one

# Restart UE
vagrant ssh vm3 -c 'sudo pkill nr-ue'
# Then start again
```

### Issue: "UPF not associated with SMF"

**Cause**: PFCP association failed between SMF and UPF.

**Solution**:
```bash
# Restart SMF
vagrant ssh ns -c 'microk8s kubectl delete pod -n free5gc -l app.kubernetes.io/component=smf --force'

# Wait and check associations
sleep 30
vagrant ssh ns -c 'microk8s kubectl logs deploy/free5gc-v1-free5gc-smf-smf -n free5gc | grep -E "PFCP|Association"'
```

### Useful Debug Commands

```bash
# Check SMF logs for UPF selection
vagrant ssh ns -c 'microk8s kubectl logs deploy/free5gc-v1-free5gc-smf-smf -n free5gc | grep -E "DNAI|Selected UPF|edge"'

# Check traffic influence data in MongoDB
vagrant ssh ns -c 'microk8s kubectl exec -n free5gc deploy/free5gc-v1-mongodb -- \
  mongosh free5gc --quiet --eval "db[\"applicationData.influenceData\"].find().pretty()"'

# Check PCF logs for influence notifications
vagrant ssh ns -c 'microk8s kubectl logs deploy/free5gc-v1-free5gc-pcf-pcf -n free5gc | grep -E "influence|DNAI"'

# Check UE registration status
vagrant ssh vm3 -c 'cat /tmp/ue.log | grep -E "Registration|PDU Session"'
```

---

## Quick Reference: Switching Traffic Routes

### Switch to edge1 (AnchorUPF1)

```bash
# 1. Delete existing subscriptions
vagrant ssh ns -c 'curl -s -X DELETE http://<NEF-IP>:80/3gpp-traffic-influence/v1/test-agent/subscriptions/<ID>'

# 2. Create edge1 subscription
vagrant ssh ns -c "curl -s -X POST http://<NEF-IP>:80/3gpp-traffic-influence/v1/test-agent/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{\"afServiceId\":\"test\",\"afAppId\":\"steering-test\",\"dnn\":\"internet\",\"snssai\":{\"sst\":1,\"sd\":\"010203\"},\"anyUeInd\":true,\"trafficFilters\":[{\"flowId\":1,\"flowDescriptions\":[\"permit out ip from any to any\"]}],\"trafficRoutes\":[{\"dnai\":\"edge1\"}]}'"

# 3. Restart UE
vagrant ssh vm3 -c 'sudo pkill nr-ue; sleep 2; cd ~/ue/UERANSIM && sudo nohup ./build/nr-ue -c config/free5gc-ue.yaml > /tmp/ue.log 2>&1 &'

# 4. Verify (should get 10.1.0.x IP)
sleep 10
vagrant ssh vm3 -c 'ip addr show uesimtun0 | grep inet'
```

### Switch to edge2 (AnchorUPF2)

```bash
# 1. Delete existing subscriptions
vagrant ssh ns -c 'curl -s -X DELETE http://<NEF-IP>:80/3gpp-traffic-influence/v1/test-agent/subscriptions/<ID>'

# 2. Create edge2 subscription
vagrant ssh ns -c "curl -s -X POST http://<NEF-IP>:80/3gpp-traffic-influence/v1/test-agent/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{\"afServiceId\":\"test\",\"afAppId\":\"steering-test\",\"dnn\":\"internet\",\"snssai\":{\"sst\":1,\"sd\":\"010203\"},\"anyUeInd\":true,\"trafficFilters\":[{\"flowId\":1,\"flowDescriptions\":[\"permit out ip from any to any\"]}],\"trafficRoutes\":[{\"dnai\":\"edge2\"}]}'"

# 3. Restart UE
vagrant ssh vm3 -c 'sudo pkill nr-ue; sleep 2; cd ~/ue/UERANSIM && sudo nohup ./build/nr-ue -c config/free5gc-ue.yaml > /tmp/ue.log 2>&1 &'

# 4. Verify (should get 10.1.128.x IP)
sleep 10
vagrant ssh vm3 -c 'ip addr show uesimtun0 | grep inet'
```

---

## Summary

1. **Configuration**: DNAI must be configured as `dnaiList` array in SMF config
2. **UE Routing**: Must include paths to all anchor UPFs for ULCL to work
3. **Traffic Influence**: Created via NEF API, stored in UDR, processed by PCF/SMF
4. **Verification**: Check UE IP address to confirm which anchor UPF was selected
5. **Restarts**: Often required after config changes (SMF, UPFs, UE)

The IP address assigned to the UE directly indicates which anchor UPF was selected:
- `10.1.0.x` → AnchorUPF1 (edge1)
- `10.1.128.x` → AnchorUPF2 (edge2)
