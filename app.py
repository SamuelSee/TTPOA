"""
Minimal backend stub for Adyen Tap to Pay on Android.

Purpose:
  Your Android app gets a `setupToken` from the Mobile SDK's
  AuthenticationProvider.authenticate(setupToken) callback. The SDK cannot
  call Adyen's /auth/certificate endpoint directly (it must go through your
  backend, using your Checkout-webservice API key). This script does that
  proxy call and returns `sdkData` back to the app.

Setup:
  1. pip install flask requests
  2. Fill in ADYEN_API_KEY and MERCHANT_ACCOUNT below
     (ADYEN_API_KEY = the credential with the "Checkout webservice" role,
      NOT the SDK-download-only key you put in settings.gradle)
  3. Run: python app.py
  4. In your Android app, point the AuthenticationProvider's backend call
     at http://<your-machine-ip>:5000/establish-session

     Note: "localhost" won't work from a physical phone. Use your machine's
     LAN IP (e.g. 192.168.x.x) and make sure the phone is on the same
     network, or use a tool like ngrok to tunnel it.
"""

import os
import time
from collections import deque

import requests
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# --- Fill these in ---
ADYEN_API_KEY = os.environ.get("ADYEN_API_KEY", "REPLACE_WITH_YOUR_CHECKOUT_WEBSERVICE_API_KEY")
MERCHANT_ACCOUNT = os.environ.get("ADYEN_MERCHANT_ACCOUNT", "REPLACE_WITH_YOUR_MERCHANT_ACCOUNT")

# Optional: set this to require a shared secret before the dashboard/API will
# do anything. Leave blank to disable (fine for local-only testing; set this
# before you host this publicly, even for "just internal" use).
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "")
# ---------------------

ADYEN_AUTH_CERTIFICATE_URL = "https://softposconfig-test.adyen.com/softposconfig/v3/auth/certificate"

# In-memory log of recent activity, just for the dashboard. Resets on
# restart -- this is a dev tool, not a persistence layer.
_recent_calls = deque(maxlen=50)

# Holds payments waiting to be picked up by the phone
# Format: {"device_1": {"amount": "10.00", "currency": "EUR", "status": "pending"}}
_pending_payments = {}


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
    """
    Expects JSON body from your Android app:
      { "setupToken": "<token from AuthenticationProvider.authenticate()>" }

    Returns JSON:
      { "sdkData": "...", "installationId": "..." }
    on success, or an error object on failure.
    """
    if not _check_secret():
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    setup_token = body.get("setupToken")

    print(f"\n--- /establish-session called ---")
    print(f"  setupToken (first 12 chars): {setup_token[:12] if setup_token else None}...")

    if not setup_token:
        print("  -> REJECTED: no setupToken in request body")
        return jsonify({"error": "Missing 'setupToken' in request body"}), 400

    payload = {
        "merchantAccount": MERCHANT_ACCOUNT,
        "setupToken": setup_token,
        # Add "store": "<storeReference>" here if your account structure
        # uses stores. Add "subMerchantData": {...} instead if you are a
        # registered payment facilitator. See Adyen docs for details.
    }
    print(f"  merchantAccount: {MERCHANT_ACCOUNT}")
    print(f"  API key (first 10 chars): {ADYEN_API_KEY[:10]}...")

    headers = {
        "x-api-key": ADYEN_API_KEY,
        "content-type": "application/json",
    }

    try:
        response = requests.post(
            ADYEN_AUTH_CERTIFICATE_URL,
            json=payload,
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  -> REQUEST FAILED: {e}")
        _log_call("establish-session", payload, "error", str(e))
        return jsonify({"error": f"Request to Adyen failed: {e}"}), 502

    print(f"  Adyen responded: {response.status_code}")
    print(f"  Adyen body: {response.text}")

    if response.status_code != 201:
        # Surface Adyen's error body so you can see what went wrong
        # (e.g. missing MCC config, bad merchant account, disabled account).
        _log_call("establish-session", payload, response.status_code, response.text)
        return jsonify({
            "error": "Adyen returned a non-201 response",
            "status_code": response.status_code,
            "adyen_response": response.text,
        }), 502

    data = response.json()
    _log_call("establish-session", payload, response.status_code, data)

    return jsonify({
        "sdkData": data.get("sdkData"),
        "installationId": data.get("installationId"),
    }), 200


@app.route("/api/pos/charge", methods=["POST"])
def pos_charge():
    """Web UI calls this to stage a payment for the phone."""
    if not _check_secret():
        return jsonify({"error": "unauthorized"}), 401
    
    body = request.get_json(silent=True) or {}
    amount = body.get("amount", "0")
    currency = body.get("currency", "EUR")
    
    # In a real app, you'd target a specific phone/terminal ID. 
    # For this stub, we'll use a hardcoded device ID: "device_1"
    _pending_payments["device_1"] = {
        "amount": amount,
        "currency": currency,
        "status": "pending"
    }
    
    _log_call("pos-charge", body, 200, {"status": "payment_staged"})
    return jsonify({"message": "Payment staged for device."}), 200


@app.route("/api/device/poll", methods=["GET"])
def device_poll():
    """Android app calls this repeatedly to check for pending payments."""
    payment = _pending_payments.get("device_1")
    
    if payment and payment["status"] == "pending":
        # Mark as processing so we don't send it twice
        payment["status"] = "processing"
        return jsonify({
            "has_payment": True,
            "amount": payment["amount"],
            "currency": payment["currency"]
        }), 200
        
    return jsonify({"has_payment": False}), 200


@app.route("/payment-result", methods=["POST"])
def payment_result():
    """
    Expects the raw SaleToPOIResponse JSON from the app's transaction
    result callback (e.g. PaymentSampleAppFragment's onPaymentFinished).

    This is purely for logging/visibility on the backend -- the SDK already
    processed the actual payment directly with Adyen's Terminal API before
    this call happens. Nothing here affects the transaction outcome.
    """
    if not _check_secret():
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}

    print(f"\n--- /payment-result received ---")
    print(f"  {body}")

    # Pull out a couple of useful fields if present, for the dashboard log.
    try:
        payment_response = body.get("SaleToPOIResponse", {}).get("PaymentResponse", {})
        result = payment_response.get("Response", {}).get("Result")
        error_condition = payment_response.get("Response", {}).get("ErrorCondition")
        summary = {"result": result, "errorCondition": error_condition}
    except Exception:
        summary = {}

    _log_call("payment-result", summary, 200, body)

    return jsonify({"status": "logged"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/api/config-check", methods=["GET"])
def config_check():
    """Tells the dashboard whether ADYEN_API_KEY / MERCHANT_ACCOUNT still
    look like placeholders, so it can warn you instead of silently failing."""
    return jsonify({
        "api_key_set": "REPLACE_WITH" not in ADYEN_API_KEY,
        "merchant_account_set": "REPLACE_WITH" not in MERCHANT_ACCOUNT,
        "merchant_account": MERCHANT_ACCOUNT if "REPLACE_WITH" not in MERCHANT_ACCOUNT else None,
    }), 200


@app.route("/api/recent-calls", methods=["GET"])
def recent_calls():
    if not _check_secret():
        return jsonify({"error": "unauthorized"}), 401
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
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 760px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #666; font-size: 14px; margin-bottom: 28px; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 18px 20px; margin-bottom: 18px; }
  .card h2 { font-size: 15px; margin: 0 0 10px; }
  .row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
  input[type=text], input[type=number] { flex: 1; padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; }
  button { padding: 8px 16px; border: none; border-radius: 6px; background: #0a5cff;
           color: white; font-size: 13px; cursor: pointer; }
  button:hover { background: #0847cc; }
  button.secondary { background: #eee; color: #333; }
  button.secondary:hover { background: #ddd; }
  pre { background: #f6f6f6; border-radius: 6px; padding: 10px 12px; font-size: 12px;
        overflow-x: auto; white-space: pre-wrap; word-break: break-all; }
  .status-ok { color: #0a7d3c; font-weight: 600; }
  .status-bad { color: #c22; font-weight: 600; }
  .log-entry { border-bottom: 1px solid #eee; padding: 10px 0; font-size: 12px; }
  .log-entry:last-child { border-bottom: none; }
  .muted { color: #888; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px;
           background: #eee; margin-right: 6px; }
  .pos-section { background: #eef5ff; border-color: #bbd6fe; }
</style>
</head>
<body>

<h1>Tap to Pay - backend test console</h1>
<div class="sub">Talks directly to this Flask backend.</div>

<!-- NEW POS SECTION -->
<div class="card pos-section">
  <h2>🛒 Web Point of Sale (Send Payment to Phone)</h2>
  <div class="sub" style="margin-bottom:10px;">
    Enter an amount here. Payment will be staged first. The App is then programmed to check <code>/api/device/poll</code> to pick up this request and start the NFC reader.
  </div>
  <div class="row">
    <input type="number" id="posAmount" placeholder="Amount (e.g. 5.00)" value="5.00">
    <input type="text" id="posCurrency" placeholder="Currency" value="EUR" style="max-width: 80px;">
    <button onclick="sendToPhone()">Send to Phone</button>
  </div>
  <pre id="posResult" style="display:none; margin-top:12px;"></pre>
</div>

<div class="card">
  <h2>Backend status</h2>
  <div id="status">checking...</div>
</div>

<div class="card" style="display:none;">
  <h2>Dashboard secret</h2>
  <div class="row">
    <input type="text" id="dashSecret" placeholder="x-dashboard-secret value">
  </div>
</div>

<div class="card">
  <h2>Test /establish-session</h2>
  <div class="row">
    <input type="text" id="setupToken" placeholder="setupToken">
  </div>
  <button onclick="testEstablish()">Send</button>
  <pre id="establishResult" style="display:none; margin-top:12px;"></pre>
</div>

<div class="card">
  <h2>Recent activity</h2>
  <button class="secondary" onclick="loadRecent()">Refresh</button>
  <div id="recentList" style="margin-top:12px;"></div>
</div>

<script>
async function sendToPhone() {
  const amount = document.getElementById('posAmount').value;
  const currency = document.getElementById('posCurrency').value;
  const secret = document.getElementById('dashSecret').value.trim();
  const out = document.getElementById('posResult');
  out.style.display = 'block';
  out.textContent = 'Sending to queue...';
  
  try {
    const res = await fetch('/api/pos/charge', {
      method: 'POST',
      headers: {'content-type': 'application/json', 'x-dashboard-secret': secret},
      body: JSON.stringify({amount, currency})
    });
    const data = await res.json();
    out.textContent = 'Success: ' + data.message + '\\n\\n(Now your Android app needs to poll /api/device/poll to see it)';
  } catch (e) {
    out.textContent = 'Error: ' + e;
  }
  loadRecent();
}

async function loadStatus(attempt) {
  attempt = attempt || 1;
  const el = document.getElementById('status');
  try {
    const cfg = await fetch('/api/config-check').then(r => r.json());
    let html = '<span class="status-ok">Server running</span><br>';
    html += cfg.api_key_set ? '<span class="status-ok">API key configured</span><br>' : '<span class="status-bad">API key missing</span><br>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<span class="status-bad">Cannot reach backend</span>';
  }
}

async function testEstablish() {
  const setupToken = document.getElementById('setupToken').value.trim();
  const secret = document.getElementById('dashSecret').value.trim();
  const out = document.getElementById('establishResult');
  out.style.display = 'block';
  try {
    const res = await fetch('/establish-session', {
      method: 'POST',
      headers: {'content-type': 'application/json', 'x-dashboard-secret': secret},
      body: JSON.stringify({setupToken})
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
    if (!calls.length) { el.innerHTML = '<span class="muted">No calls yet.</span>'; return; }
    el.innerHTML = calls.map(c => `
      <div class="log-entry">
        <span class="badge">${c.time}</span> <span class="badge">${c.kind}</span>
        <pre>${JSON.stringify(c.response, null, 2)}</pre>
      </div>
    `).join('');
  } catch (e) {
    el.innerHTML = '<span class="status-bad">Could not load activity</span>';
  }
}

loadStatus();
loadRecent();
</script>

</body>
</html>
"""


if __name__ == "__main__":
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)  # hides the bare "POST ... 200 -" lines

    # host="0.0.0.0" so your phone (on the same LAN) can reach this machine.
    app.run(host="0.0.0.0", port=5000, debug=True)