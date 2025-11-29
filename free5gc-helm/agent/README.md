# Traffic Steering Agent

A REST API that automates the traffic influence subscription process for free5gc. This replaces the manual `curl` commands from the [free5gc traffic steering tutorial](https://free5gc.org/blog/20250625/20250625/).

## Quick Start

### 1. Build and Push the Image

```bash
cd agent

# Build the Docker image
docker build -t traffic-steering-agent:v1 .

# Tag and push to MicroK8s local registry
docker tag traffic-steering-agent:v1 localhost:32000/traffic-steering-agent:v1
docker push localhost:32000/traffic-steering-agent:v1
```

### 2. Deploy to Kubernetes

```bash
kubectl apply -f agent-deployment.yaml
```

### 3. Test the API

```bash
# Check health
curl http://<node-ip>:30080/health

# Get current config
curl http://<node-ip>:30080/config

# List existing subscriptions
curl http://<node-ip>:30080/subscriptions

# Create a traffic steering subscription (simplified)
curl -X POST http://<node-ip>:30080/steer \
  -H "Content-Type: application/json" \
  -d '{"target_ip": "10.0.2.105", "dnai": "mec"}'

# Or use the full ti_data format
curl -X POST http://<node-ip>:30080/subscriptions \
  -H "Content-Type: application/json" \
  -d @ti_data.json

# Delete a subscription
curl -X DELETE http://<node-ip>:30080/subscriptions/1
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/config` | Get current configuration |
| GET | `/subscriptions` | List all traffic influence subscriptions |
| POST | `/subscriptions` | Create a new subscription (full payload) |
| GET | `/subscriptions/<id>` | Get a specific subscription |
| DELETE | `/subscriptions/<id>` | Delete a subscription |
| POST | `/steer` | Simplified: Create traffic steering to a target |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NEF_URL` | `http://nef-service.free5gc.svc.cluster.local:80` | NEF service URL |
| `AF_ID` | `af001` | Application Function ID |
| `DEFAULT_DNN` | `internet` | Default Data Network Name |
| `DEFAULT_SNSSAI_SST` | `1` | Default Slice/Service Type |
| `DEFAULT_SNSSAI_SD` | `010203` | Default Slice Differentiator |
| `UE_SUBNET` | `10.1.0.0/24` | UE subnet for traffic filters |
| `PORT` | `8080` | API server port |

## Example: Steer Traffic to MEC App

```bash
# Simple steering to MEC app at 10.0.2.105
curl -X POST http://192.168.56.119:30080/steer \
  -H "Content-Type: application/json" \
  -d '{
    "target_ip": "10.0.2.105",
    "dnai": "mec"
  }'
```

This is equivalent to the manual step from the tutorial:
```bash
curl -X POST -H "Content-Type: application/json" \
  --data @./ti_data.json \
  http://10.152.183.217/3gpp-traffic-influence/v1/af001/subscriptions
```

## Example ti_data.json

```json
{
    "afServiceId": "Service1",
    "dnn": "internet",
    "snssai": {
        "sst": 1,
        "sd": "010203"
    },
    "anyUeInd": true,
    "notificationDestination": "http://agent:8080/callback",
    "trafficFilters": [
        {
            "flowId": 1,
            "flowDescriptions": [
                "permit out ip from 10.0.2.105 to 10.1.0.0/24"
            ]
        }
    ],
    "trafficRoutes": [
        {
            "dnai": "mec"
        }
    ]
}
```

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export NEF_URL="http://localhost:8000"  # or your NEF service
export AF_ID="af001"

# Run the API server
python api.py

# Or run with debug mode
DEBUG=true python api.py
```

## Architecture

```
┌─────────────┐       ┌─────────────────┐       ┌─────────────┐
│   Client    │──────▶│  Agent API      │──────▶│    NEF      │
│  (curl/UI)  │       │  (this service) │       │  (free5gc)  │
└─────────────┘       └─────────────────┘       └──────┬──────┘
                                                       │
                                                       ▼
                                                ┌─────────────┐
                                                │    SMF      │
                                                │  (routing)  │
                                                └──────┬──────┘
                                                       │
                                                       ▼
                                                ┌─────────────┐
                                                │    UPF      │
                                                │ (data plane)│
                                                └─────────────┘
```

## Next Steps

- [ ] Add authentication
- [ ] Add rate limiting
- [ ] Integrate AI decision-making (main.py)
- [ ] Add Prometheus metrics for API calls
- [ ] Create a simple web UI
