"""
Traffic Steering API Agent
===========================
A simple REST API that wraps the 3GPP Traffic Influence API exposed by the NEF.
This allows testing traffic steering in the free5gc lab without manual curl commands.

Based on: https://free5gc.org/blog/20250625/20250625/
"""

import os
import json
import logging
from flask import Flask, request, jsonify
import requests

# --- Configuration ---
NEF_URL = os.getenv("NEF_URL", "http://nef-service.free5gc.svc.cluster.local:80")
AF_ID = os.getenv("AF_ID", "af001")
DEFAULT_DNN = os.getenv("DEFAULT_DNN", "internet")
DEFAULT_SNSSAI_SST = int(os.getenv("DEFAULT_SNSSAI_SST", "1"))
DEFAULT_SNSSAI_SD = os.getenv("DEFAULT_SNSSAI_SD", "010203")
UE_SUBNET = os.getenv("UE_SUBNET", "10.1.0.0/24")

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)


# --- Helper Functions ---

def get_nef_base_url():
    """Returns the base URL for NEF traffic influence API."""
    return f"{NEF_URL}/3gpp-traffic-influence/v1/{AF_ID}"


def build_ti_subscription(
    target_ip: str,
    dnai: str = "mec",
    dnn: str = None,
    sst: int = None,
    sd: str = None,
    af_service_id: str = "TrafficSteeringAgent",
    flow_id: int = 1,
    ue_subnet: str = None
) -> dict:
    """
    Build a Traffic Influence subscription payload.
    
    Args:
        target_ip: The IP address to steer traffic TO (e.g., MEC app IP)
        dnai: Data Network Access Identifier (e.g., "mec", "mec2")
        dnn: Data Network Name (e.g., "internet", "internet2")
        sst: Slice/Service Type
        sd: Slice Differentiator
        af_service_id: Application Function Service ID
        flow_id: Traffic filter flow ID
        ue_subnet: UE subnet for traffic filter
    
    Returns:
        dict: The ti_data payload for NEF API
    """
    return {
        "afServiceId": af_service_id,
        "dnn": dnn or DEFAULT_DNN,
        "snssai": {
            "sst": sst or DEFAULT_SNSSAI_SST,
            "sd": sd or DEFAULT_SNSSAI_SD
        },
        "anyUeInd": True,
        "notificationDestination": f"http://agent:8080/callback",
        "trafficFilters": [
            {
                "flowId": flow_id,
                "flowDescriptions": [
                    f"permit out ip from {target_ip} to {ue_subnet or UE_SUBNET}"
                ]
            }
        ],
        "trafficRoutes": [
            {
                "dnai": dnai
            }
        ]
    }


# --- API Endpoints ---

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "nef_url": NEF_URL, "af_id": AF_ID})


@app.route('/config', methods=['GET'])
def get_config():
    """Get current configuration."""
    return jsonify({
        "nef_url": NEF_URL,
        "af_id": AF_ID,
        "default_dnn": DEFAULT_DNN,
        "default_snssai": {"sst": DEFAULT_SNSSAI_SST, "sd": DEFAULT_SNSSAI_SD},
        "ue_subnet": UE_SUBNET
    })


@app.route('/subscriptions', methods=['GET'])
def list_subscriptions():
    """
    List all traffic influence subscriptions.
    
    Query params:
        dnn: Filter by DNN (optional)
    """
    url = f"{get_nef_base_url()}/subscriptions"
    params = {}
    
    dnn = request.args.get('dnn')
    if dnn:
        params['dnns'] = dnn
    
    try:
        logger.info(f"GET {url} params={params}")
        resp = requests.get(url, params=params, headers={'Content-Type': 'application/json'}, timeout=10)
        
        return jsonify({
            "status": "success",
            "status_code": resp.status_code,
            "data": resp.json() if resp.text else None
        }), resp.status_code
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to list subscriptions: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/subscriptions', methods=['POST'])
def create_subscription():
    """
    Create a new traffic influence subscription.
    
    Request body (JSON):
        target_ip: IP address to steer traffic to (required)
        dnai: Data Network Access Identifier (default: "mec")
        dnn: Data Network Name (default: from config)
        sst: Slice/Service Type (default: from config)
        sd: Slice Differentiator (default: from config)
        ue_subnet: UE subnet (default: from config)
    
    OR you can pass the full ti_data payload directly.
    """
    data = request.get_json()
    
    if not data:
        return jsonify({"status": "error", "message": "Request body required"}), 400
    
    # Check if this is a full ti_data payload or simplified params
    if 'trafficRoutes' in data:
        # Full payload provided
        ti_data = data
    else:
        # Simplified params - build the payload
        target_ip = data.get('target_ip')
        if not target_ip:
            return jsonify({"status": "error", "message": "target_ip is required"}), 400
        
        ti_data = build_ti_subscription(
            target_ip=target_ip,
            dnai=data.get('dnai', 'mec'),
            dnn=data.get('dnn'),
            sst=data.get('sst'),
            sd=data.get('sd'),
            ue_subnet=data.get('ue_subnet')
        )
    
    url = f"{get_nef_base_url()}/subscriptions"
    
    try:
        logger.info(f"POST {url}")
        logger.info(f"Payload: {json.dumps(ti_data, indent=2)}")
        
        resp = requests.post(
            url, 
            json=ti_data, 
            headers={'Content-Type': 'application/json'}, 
            timeout=10
        )
        
        result = {
            "status": "success" if resp.status_code in [200, 201] else "failed",
            "status_code": resp.status_code,
            "request_payload": ti_data
        }
        
        if resp.text:
            try:
                result["response"] = resp.json()
            except:
                result["response_text"] = resp.text
        
        logger.info(f"Response: {resp.status_code}")
        return jsonify(result), resp.status_code
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to create subscription: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/subscriptions/<sub_id>', methods=['GET'])
def get_subscription(sub_id):
    """Get a specific subscription by ID."""
    url = f"{get_nef_base_url()}/subscriptions/{sub_id}"
    
    try:
        logger.info(f"GET {url}")
        resp = requests.get(url, headers={'Content-Type': 'application/json'}, timeout=10)
        
        return jsonify({
            "status": "success",
            "status_code": resp.status_code,
            "data": resp.json() if resp.text else None
        }), resp.status_code
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to get subscription: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/subscriptions/<sub_id>', methods=['DELETE'])
def delete_subscription(sub_id):
    """Delete a subscription by ID."""
    url = f"{get_nef_base_url()}/subscriptions/{sub_id}"
    
    try:
        logger.info(f"DELETE {url}")
        resp = requests.delete(url, headers={'Content-Type': 'application/json'}, timeout=10)
        
        return jsonify({
            "status": "success" if resp.status_code in [200, 204] else "failed",
            "status_code": resp.status_code,
            "message": f"Subscription {sub_id} deleted" if resp.status_code in [200, 204] else resp.text
        }), resp.status_code
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to delete subscription: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/steer', methods=['POST'])
def steer_traffic():
    """
    Convenience endpoint: Create traffic steering to a specific target.
    
    This is a simplified endpoint that creates a subscription with sensible defaults.
    
    Request body (JSON):
        target_ip: IP address of MEC app or destination (required)
        dnai: Data Network Access Identifier (default: "mec")
        dnn: Data Network Name (default: "internet")
    
    Example:
        POST /steer
        {"target_ip": "10.0.2.105", "dnai": "mec"}
    """
    data = request.get_json()
    
    if not data or 'target_ip' not in data:
        return jsonify({
            "status": "error", 
            "message": "target_ip is required",
            "example": {"target_ip": "10.0.2.105", "dnai": "mec"}
        }), 400
    
    ti_data = build_ti_subscription(
        target_ip=data['target_ip'],
        dnai=data.get('dnai', 'mec'),
        dnn=data.get('dnn'),
        sst=data.get('sst'),
        sd=data.get('sd'),
        ue_subnet=data.get('ue_subnet')
    )
    
    url = f"{get_nef_base_url()}/subscriptions"
    
    try:
        logger.info(f"Steering traffic to {data['target_ip']} via dnai={data.get('dnai', 'mec')}")
        logger.info(f"POST {url}")
        
        resp = requests.post(
            url, 
            json=ti_data, 
            headers={'Content-Type': 'application/json'}, 
            timeout=10
        )
        
        success = resp.status_code in [200, 201]
        
        result = {
            "status": "success" if success else "failed",
            "message": f"Traffic steering to {data['target_ip']} {'activated' if success else 'failed'}",
            "status_code": resp.status_code,
            "ti_data": ti_data
        }
        
        if resp.text:
            try:
                result["nef_response"] = resp.json()
            except:
                result["nef_response_text"] = resp.text
        
        return jsonify(result), resp.status_code
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to steer traffic: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/callback', methods=['POST'])
def nef_callback():
    """
    Callback endpoint for NEF notifications.
    The NEF may send notifications here when subscription status changes.
    """
    data = request.get_json()
    logger.info(f"Received NEF callback: {json.dumps(data, indent=2)}")
    return jsonify({"status": "received"}), 200


# --- Main ---

if __name__ == '__main__':
    port = int(os.getenv('PORT', '8080'))
    debug = os.getenv('DEBUG', 'false').lower() == 'true'
    
    logger.info(f"Starting Traffic Steering API Agent on port {port}")
    logger.info(f"NEF URL: {NEF_URL}")
    logger.info(f"AF ID: {AF_ID}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)
