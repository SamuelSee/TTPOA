import os
import json
import time
from collections import deque
import requests
from flask import Flask, request, jsonify, Response
import firebase_admin
from firebase_admin import credentials, messaging

# THIS IS THE LINE GUNICORN WAS LOOKING FOR
app = Flask(__name__)

# --- Firebase setup ---
_firebase_ready = False
try:
    firebase_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    if firebase_json:
        cred = credentials.Certificate(json.loads(firebase_json))
        firebase_admin.initialize_app(cred)
        _firebase_ready = True
        print("--- Firebase initialized successfully ---")
    else:
        print("--- FIREBASE_SERVICE_ACCOUNT_JSON not set, push notifications disabled ---")
except Exception as e:
    print(f"--- Firebase initialization failed: {e} ---")

_device_fcm_token = {"token": None}

ADYEN_API_KEY = os.environ.get("ADYEN_API_KEY", "REPLACE_WITH_YOUR_CHECKOUT_WEBSERVICE_API_KEY")
MERCHANT_ACCOUNT = os.environ.get("ADYEN_MERCHANT_ACCOUNT", "REPLACE_WITH_YOUR_MERCHANT_ACCOUNT")
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "")

ADYEN_AUTH_CERTIFICATE_URL = "https://softposconfig-test.adyen.com/softposconfig/v3/auth/certificate"

_recent_calls = deque(maxlen=50)

_device_queue = {
    "has_payment": False,
    "amount": None,
    "currency": None,
    "should_reset": False,
}

def _log_call(kind, request_body, response_status, response_body):
    _recent_calls.appendleft({
        "time": time.strftime("%H:%M:%S"),
        "kind": kind,
        "request": request_body,
        "status": response_status,
        "response": response_body,
    })

def _check_secret():
    if not DASHBOARD_SECRET:
        return True
    return request.headers.get("x-dashboard-secret") == DASHBOARD_SECRET

@app.route("/establish-session", methods=["POST"])
def establish_session():
    if not _check_secret(): return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    setup_token = body.get("setupToken")
    if not setup_token: return jsonify({"error": "Missing 'setupToken'"}), 400

    payload = {"merchantAccount": MERCHANT_ACCOUNT, "setupToken": setup_token}
    headers = {"x-api-key": ADYEN_API_KEY, "content-type": "application/json"}

    try:
        response = requests.post(ADYEN_AUTH_CERTIFICATE_URL, json=payload, headers=headers, timeout=15)
    except requests.RequestException as e:
        return jsonify({"error": f"Request to Adyen failed: {e}"}), 502

    if response.status_code != 201:
        _log_call("establish-session", payload, response.status_code, response.text)
        return jsonify({"error": "Adyen returned a non-201 response", "status_code": response.status_code, "adyen_response": response.text}), 502

    data = response.json()
    _log_call("establish-session", payload, response.status_code, data)
    return jsonify({"sdkData": data.get("sdkData"), "installationId": data.get("installationId")}), 200

@app.route("/payment-result", methods=["POST"])
def payment_result():
    if not _check_secret(): return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    _log_call("payment-result", {}, 200, body)
    return jsonify({"status": "logged"}), 200

@app.route("/api/device/register-token", methods=["POST"])
def register_token():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    if not token: return jsonify({"error": "Missing 'token'"}), 400
    _device_fcm_token["token"] = token
    return jsonify({"status": "registered"}), 200

@app.route("/api/device/push-payment", methods=["POST"])
def push_payment():
    if not _check_secret(): return jsonify({"error": "unauthorized"}), 401
    if not _firebase_ready: return jsonify({"error": "Firebase not configured"}), 500
    
    token = _device_fcm_token.get("token")
    if not token: return jsonify({"error": "No device registered FCM yet"}), 400

    body = request.get_json(silent=True) or {}
    amount = body.get("amount", "5")
    currency = body.get("currency", "EUR")

    message = messaging.Message(
        data={"amount": str(amount), "currency": str(currency), "should_reset": "false"},
        token=token,
        android=messaging.AndroidConfig(priority="high"),
    )

    try:
        response = messaging.send(message)
        _log_call("push-payment", {"amount": amount, "currency": currency}, 200, response)
        return jsonify({"status": "sent", "message_id": response}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to send push: {e}"}), 502

@app.route("/api/device/push-reset", methods=["POST"])
def push_reset():
    if not _check_secret(): return jsonify({"error": "unauthorized"}), 401
    if not _firebase_ready: return jsonify({"error": "Firebase not configured"}), 500
    
    token = _device_fcm_token.get("token")
    if not token: return jsonify({"error": "No device registered FCM yet"}), 400

    message = messaging.Message(
        data={"should_reset": "true"},
        token=token,
        android=messaging.AndroidConfig(priority="high"),
    )

    try:
        response = messaging.send(message)
        _log_call("push-reset", {}, 200, response)
        return jsonify({"status": "sent", "message_id": response}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to send push: {e}"}), 502

@app.route("/health", methods=["GET"])
def health(): return jsonify({"status": "ok"}), 200

@app.route("/api/config-check", methods=["GET"])
def config_check():
    return jsonify({
        "api_key_set": "REPLACE_WITH" not in ADYEN_API_KEY,
        "merchant_account_set": "REPLACE_WITH" not in MERCHANT_ACCOUNT,
        "merchant_account": MERCHANT_ACCOUNT if "REPLACE_WITH" not in MERCHANT_ACCOUNT else None,
    }), 200

@app.route("/api/recent-calls", methods=["GET"])
def recent_calls():
    if not _check_secret(): return jsonify({"error": "unauthorized"}), 401
    return jsonify(list(_recent_calls)), 200

@app.route("/", methods=["GET"])
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")


DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Adyen Tap to Pay - test console</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 760px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #666; font-size: 14px; margin-bottom: 28px; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 18px 20px; margin-bottom: 18px; }
  .card h2 { font-size: 15px; margin: 0 0 10px; }
  .row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
  input[type=text] { flex: 1; padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; }
  button { padding: 8px 16px; border: none; border-radius: 6px; background: #0a5cff; color: white; font-size: 13px; cursor: pointer; }
  button:hover { background: #0847cc; }
  button.secondary { background: #eee; color: #333; }
  button.secondary:hover { background: #ddd; }
  pre { background: #f6f6f6; border-radius: 6px; padding: 10px 12px; font-size: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; }
  .status-ok { color: #0a7d3c; font-weight: 600; }
  .status-bad { color: #c22; font-weight: 600; }
  .log-entry { border-bottom: 1px solid #eee; padding: 10px 0; font-size: 12px; }
  .log-entry:last-child { border-bottom: none; }
  .muted { color: #888; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; background: #eee; margin-right: 6px; }
</style>
</head>
<body>

<h1>Tap to Pay - backend test console</h1>

<div class="card">
  <h2>Dashboard secret</h2>
  <div class="row">
    <input type="text" id="dashSecret" placeholder="x-dashboard-secret value (leave blank if not set)">
  </div>
</div>

<div class="card">
  <h2>Remote Push Trigger (FCM)</h2>
  <div class="sub" style="margin-bottom:10px;">
    Sends an FCM push notification to wake the phone and trigger a Tap to Pay transaction immediately.
  </div>
  <div class="row">
    <input type="text" id="remoteAmount" placeholder="Amount" value="5" style="max-width:120px;">
    <input type="text" id="remoteCurrency" placeholder="Currency" value="EUR" style="max-width:100px;">
    <button onclick="pushPayment()">Send Push Payment</button>
  </div>
  <button class="secondary" onclick="pushReset()">Force session reset (Push)</button>
  <pre id="remoteResult" style="display:none; margin-top:12px;"></pre>
</div>

<div class="card">
  <h2>Recent activity</h2>
  <button class="secondary" onclick="loadRecent()">Refresh</button>
  <div id="recentList" style="margin-top:12px;"></div>
</div>

<script>
async function pushPayment() {
  const amount = document.getElementById('remoteAmount').value.trim();
  const currency = document.getElementById('remoteCurrency').value.trim();
  const secret = document.getElementById('dashSecret').value.trim();
  const out = document.getElementById('remoteResult');
  
  out.style.display = 'block';
  out.textContent = 'Sending FCM Push...';
  
  try {
    const res = await fetch('/api/device/push-payment', {
      method: 'POST',
      headers: {'content-type': 'application/json', 'x-dashboard-secret': secret},
      body: JSON.stringify({amount, currency})
    });
    const data = await res.json();
    out.textContent = 'Status: ' + res.status + '\\n\\n' + JSON.stringify(data, null, 2);
  } catch (e) {
    out.textContent = 'Request failed: ' + e;
  }
  loadRecent();
}

async function pushReset() {
  const secret = document.getElementById('dashSecret').value.trim();
  const out = document.getElementById('remoteResult');
  
  out.style.display = 'block';
  out.textContent = 'Sending FCM Push...';
  
  try {
    const res = await fetch('/api/device/push-reset', {
      method: 'POST',
      headers: {'content-type': 'application/json', 'x-dashboard-secret': secret}
    });
    const data = await res.json();
    out.textContent = 'Status: ' + res.status + '\\n\\n' + JSON.stringify(data, null, 2);
  } catch (e) {
    out.textContent = 'Request failed: ' + e;
  }
  loadRecent();
}

async function loadRecent() {
  const el = document.getElementById('recentList');
  const secret = document.getElementById('dashSecret').value.trim();
  try {
    const calls = await fetch('/api/recent-calls', {
      headers: {'x-dashboard-secret': secret}
    }).then(r => r.json());
    if (!calls.length) {
      el.innerHTML = '<span class="muted">No calls yet.</span>';
      return;
    }
    el.innerHTML = calls.map(c => `
      <div class="log-entry">
        <span class="badge">${c.time}</span>
        <span class="badge">${c.kind}</span>
        <span class="${c.status === 201 || c.status === 200 ? 'status-ok' : 'status-bad'}">status ${c.status}</span>
        <pre>${JSON.stringify(c.response, null, 2)}</pre>
      </div>
    `).join('');
  } catch (e) {
    el.innerHTML = '<span class="status-bad">Could not load activity</span>';
  }
}
loadRecent();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)