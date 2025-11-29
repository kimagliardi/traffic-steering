# Traffic Steering Agent - Technical Documentation

## Overview

The Traffic Steering Agent is a REST API that automates 3GPP Traffic Influence operations in a free5gc deployment. It wraps the NEF (Network Exposure Function) API to enable programmatic control of traffic routing decisions in a 5G network with ULCL (Uplink Classifier) topology.

This document explains how traffic steering works in 5G networks and how this agent interfaces with the free5gc core.

---

## Architecture

### Network Topology (ULCL Mode)

```
                                    ┌─────────────┐
                                    │   UPF1      │
                                    │  (Anchor)   │──────► Internet/DN
                                    │   "mec"     │
                                    └──────▲──────┘
                                           │ N9
    ┌──────────┐      ┌──────────┐    ┌────┴─────┐
    │   UE     │──────│   gNB    │────│   UPFb   │
    │(UERANSIM)│  N1  │(UERANSIM)│ N3 │(Branching│
    └──────────┘      └──────────┘    └────┬─────┘
                                           │ N9
                                    ┌──────▼──────┐
                                    │   UPF2      │
                                    │  (Anchor)   │──────► MEC/Edge
                                    │   "edge"    │
                                    └─────────────┘
```

**Key Components:**
- **UPFb (Branching UPF)**: Receives traffic from gNB and decides which anchor UPF to forward to
- **UPF1 (Anchor)**: Routes traffic to internet/central data network (DNAI: "internet")
- **UPF2 (Anchor)**: Routes traffic to edge/MEC data network (DNAI: "mec")
- **DNAI**: Data Network Access Identifier - labels for different network paths

### Control Plane Flow

```
┌────────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐     ┌─────────┐
│   Agent    │────►│   NEF   │────►│   UDR   │────►│   PCF   │────►│   SMF   │
│  (API)     │     │         │     │         │     │         │     │         │
└────────────┘     └─────────┘     └─────────┘     └─────────┘     └────┬────┘
                                                                        │
                   Traffic Influence                                    │ PFCP
                   Subscription                                         ▼
                                                                   ┌─────────┐
                                                                   │   UPF   │
                                                                   │  (PDR/  │
                                                                   │   FAR)  │
                                                                   └─────────┘
```

---

## How Traffic Steering Works

### Step 1: Traffic Influence Subscription (AF → NEF)

The Application Function (our agent) sends a **Traffic Influence Subscription** to the NEF:

```json
{
    "afServiceId": "TrafficSteeringAgent",
    "dnn": "internet",
    "snssai": {"sst": 1, "sd": "010203"},
    "anyUeInd": true,
    "trafficFilters": [{
        "flowId": 1,
        "flowDescriptions": ["permit out ip from 10.0.2.105 to 10.1.0.0/24"]
    }],
    "trafficRoutes": [{"dnai": "mec"}]
}
```

**Key Fields:**
| Field | Description |
|-------|-------------|
| `afServiceId` | Identifier for the AF service requesting traffic steering |
| `dnn` | Data Network Name (e.g., "internet") |
| `snssai` | Network slice (SST=1, SD=010203 for eMBB) |
| `anyUeInd` | Apply to all UEs (true) or specific UE |
| `trafficFilters` | IP flow descriptions to match |
| `trafficRoutes` | Target DNAI (Data Network Access Identifier) |

### Step 2: NEF → UDR (Store Influence Data)

NEF stores the traffic influence data in the UDR (Unified Data Repository):
- Creates an `influenceData` record associated with the subscription
- UDR notifies PCF about the new influence data

### Step 3: PCF Policy Update

PCF receives the notification and:
1. Matches the influence data to active PDU sessions
2. Creates/updates PCC (Policy and Charging Control) rules
3. Sends **SM Policy Update Notification** to SMF

### Step 4: SMF Session Modification

SMF receives the policy update and:
1. Calculates new PDRs (Packet Detection Rules) and FARs (Forwarding Action Rules)
2. Sends **PFCP Session Modification Request** to the UPFb

### Step 5: UPF Applies Forwarding Rules

UPFb updates its forwarding table:
- **PDR**: Matches packets based on traffic filter (source/dest IP, ports)
- **FAR**: Forwards matching packets to the specified anchor UPF (UPF2 for "mec")

---

## API Reference

### Base URL
```
http://<node-ip>:30080
```

### Endpoints

#### Health Check
```http
GET /health
```
Returns agent status and configuration.

**Response:**
```json
{
    "status": "healthy",
    "nef_url": "http://free5gc-v1-free5gc-nef-service.free5gc.svc.cluster.local:80",
    "af_id": "af001"
}
```

---

#### Get Configuration
```http
GET /config
```
Returns current agent configuration including default values.

---

#### Create Traffic Steering Subscription
```http
POST /steer
Content-Type: application/json
```

**Request Body:**
```json
{
    "target_ip": "10.0.2.105",
    "dnai": "mec",
    "dnn": "internet",
    "sst": 1,
    "sd": "010203",
    "af_service_id": "MyService",
    "flow_id": 1,
    "ue_subnet": "10.1.0.0/24"
}
```

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `target_ip` | Yes | - | Source IP to steer traffic from |
| `dnai` | No | "mec" | Target Data Network Access Identifier |
| `dnn` | No | "internet" | Data Network Name |
| `sst` | No | 1 | Slice/Service Type |
| `sd` | No | "010203" | Slice Differentiator |
| `af_service_id` | No | "TrafficSteeringAgent" | AF Service identifier |
| `flow_id` | No | 1 | Flow identifier |
| `ue_subnet` | No | "10.1.0.0/24" | Destination subnet for flow filter |

**Response (201 Created):**
```json
{
    "status": "success",
    "message": "Traffic steering to 10.0.2.105 activated",
    "status_code": 201,
    "ti_data": { ... },
    "nef_response": {
        "self": "http://.../subscriptions/1",
        ...
    }
}
```

---

#### List Subscriptions
```http
GET /subscriptions
```

Returns all active traffic influence subscriptions from NEF.

---

#### Get Subscription
```http
GET /subscriptions/{sub_id}
```

Returns details of a specific subscription.

---

#### Delete Subscription
```http
DELETE /subscriptions/{sub_id}
```

Removes a traffic steering subscription, reverting to default routing.

---

#### Callback Endpoint
```http
POST /callback
```

Receives notifications from NEF when subscription status changes.

---

## Deployment

### Kubernetes Resources

The agent is deployed as:
- **Deployment**: `traffic-steering-agent` (1 replica)
- **Service**: ClusterIP on port 80 (internal)
- **NodePort Service**: Exposed on port 30080 (external access)

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NEF_URL` | `http://free5gc-v1-free5gc-nef-service.free5gc.svc.cluster.local:80` | NEF service URL |
| `AF_ID` | `af001` | Application Function identifier |
| `DEFAULT_DNN` | `internet` | Default Data Network Name |
| `DEFAULT_SNSSAI_SST` | `1` | Default slice type |
| `DEFAULT_SNSSAI_SD` | `010203` | Default slice differentiator |
| `UE_SUBNET` | `10.1.0.0/24` | Default UE subnet |

### Build and Deploy

```bash
# Build image (from ns VM)
cd /home/vagrant/free5gc-helm/agent
sudo docker build -t localhost:32000/traffic-steering-agent:v2 .
sudo docker push localhost:32000/traffic-steering-agent:v2

# Deploy to cluster
kubectl apply -f agent-deployment.yaml

# Update image (if already deployed)
kubectl set image deployment/traffic-steering-agent -n free5gc \
    agent=localhost:32000/traffic-steering-agent:v2
```

---

## Verification

### 1. Check NEF Logs (Subscription Received)
```bash
kubectl logs -n free5gc -l app.kubernetes.io/name=free5gc-nef | \
    grep -i "subscription\|influence"
```

Expected output:
```
[INFO][NEF][TraffInfl] PostTrafficInfluenceSubscription - afID[af001]
[INFO][NEF][CTX][AFID:AF:af001][SubID:SUB:1] New subscription
```

### 2. Check PCF Logs (Policy Update)
```bash
kubectl logs -n free5gc -l app.kubernetes.io/name=free5gc-pcf | \
    grep -i "influence\|policy"
```

Expected output:
```
[INFO][PCF][Callback] Handle Influence Data Update Notify
[INFO][PCF][SMpolicy] Send SM Policy Update Notification to SMF
```

### 3. Check SMF Logs (Session Modification)
```bash
kubectl logs -n free5gc -l app.kubernetes.io/name=free5gc-smf | \
    grep -i "modify\|pfcp\|pdr\|far"
```

### 4. List Active Subscriptions
```bash
curl http://192.168.56.119:30080/subscriptions
```

---

## 3GPP Standards Reference

This implementation follows these 3GPP specifications:

| Specification | Title |
|---------------|-------|
| **TS 29.522** | 5G System; Network Exposure Function Northbound APIs |
| **TS 23.502** | Procedures for the 5G System (traffic influence) |
| **TS 23.503** | Policy and Charging Control Framework |
| **TS 29.512** | Session Management Policy Control Service |
| **TS 29.244** | Interface between CP and UP (PFCP) |

### Traffic Influence API Path
```
/3gpp-traffic-influence/v1/{afId}/subscriptions
```

---

## Example Use Cases

### 1. Steer UE Traffic to Edge/MEC
```bash
curl -X POST http://192.168.56.119:30080/steer \
    -H "Content-Type: application/json" \
    -d '{"target_ip": "10.60.0.1", "dnai": "mec"}'
```

### 2. Revert to Central Routing
```bash
# Delete the subscription
curl -X DELETE http://192.168.56.119:30080/subscriptions/1
```

### 3. Custom Traffic Filter
```bash
curl -X POST http://192.168.56.119:30080/steer \
    -H "Content-Type: application/json" \
    -d '{
        "target_ip": "10.60.0.1",
        "dnai": "mec",
        "ue_subnet": "192.168.1.0/24",
        "af_service_id": "VideoStreaming"
    }'
```

---

## Troubleshooting

### Subscription Created but Traffic Not Steered

1. **Check if UE has active PDU session**: Traffic influence only applies to active sessions
2. **Verify DNAI exists in SMF config**: The target DNAI must be configured in SMF
3. **Check traffic filter matches**: Ensure source IP and destination subnet match actual traffic

### NEF Returns 404

- Verify `AF_ID` matches NEF configuration
- Check NEF service is running: `kubectl get svc -n free5gc | grep nef`

### PCF Not Receiving Notification

- Check UDR connectivity: NEF must successfully store influence data
- Verify PCF subscription to UDR notifications

---

## Files

| File | Description |
|------|-------------|
| `api.py` | Flask REST API implementation |
| `main.py` | AI-powered agent loop (alternative mode) |
| `constants.py` | Shared configuration constants |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Container build definition |
| `agent-deployment.yaml` | Kubernetes deployment manifest |
| `ti_data.json` | Sample traffic influence payload |

---

## References

- [free5gc Traffic Steering Blog Post](https://free5gc.org/blog/20250625/20250625/)
- [free5gc ULCL Documentation](https://free5gc.org/guide/5-install-ueransim/#ulcl-mode)
- [3GPP TS 29.522 - NEF Northbound APIs](https://www.3gpp.org/ftp/Specs/archive/29_series/29.522/)
