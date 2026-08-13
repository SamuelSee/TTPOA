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
import json
import time
from collections import deque

import requests
from flask import Flask, request, jsonify, Response
import firebase_admin
from firebase_admin import credentials, messaging

app = Flask(__name__)

# --- Firebase setup ---
# FIREBASE_SERVICE_ACCOUNT_JSON should contain the *entire contents* of the
# service account JSON file downloaded from Firebase console, as one string.
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
# ---------------------

# Holds the phone's current FCM token, sent up via /api/device/register-token.
# Single-device model, same as before -- one test phone at a time.
_device_fcm_token = {"token": None}

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

# In-memory queue for the "Web POS" remote-trigger feature. The Android app
# polls /api/device/poll every ~3s; this holds whatever the dashboard queued
# up most recently. Single-device, single-pending-item model -- good enough
# for one test phone, not built for multiple devices/concurrent payments.
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


@app.route("/api/device/poll", methods=["GET"])
def device_poll():
    """
    Polled by the Android app every ~3s. Returns whatever's currently
    queued, then clears the payment flag (should_reset also clears) so the
    same command doesn't fire twice.
    """
    global _device_queue

    result = dict(_device_queue)

    if _device_queue["has_payment"] or _device_queue["should_reset"]:
        print(f"\n--- /api/device/poll: delivering queued command ---")
        print(f"  {result}")

    # Clear after delivering once.
    _device_queue["has_payment"] = False
    _device_queue["amount"] = None
    _device_queue["currency"] = None
    _device_queue["should_reset"] = False

    return jsonify(result), 200


@app.route("/api/device/queue-payment", methods=["POST"])
def queue_payment():
    """
    Call this to remotely trigger a payment on the phone. Expects JSON:
      { "amount": "10", "currency": "EUR" }
    The phone will wake, come to the foreground, and start a Tap to Pay
    transaction with these values on its next poll (within ~3s).
    """
    if not _check_secret():
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    amount = body.get("amount", "5")
    currency = body.get("currency", "EUR")

    _device_queue["has_payment"] = True
    _device_queue["amount"] = amount
    _device_queue["currency"] = currency

    print(f"\n--- /api/device/queue-payment: queued {amount} {currency} ---")
    _log_call("queue-payment", {"amount": amount, "currency": currency}, 200, "queued")

    return jsonify({"status": "queued", "amount": amount, "currency": currency}), 200


@app.route("/api/device/queue-reset", methods=["POST"])
def queue_reset():
    """Call this to remotely force the phone to clear/re-establish its session."""
    if not _check_secret():
        return jsonify({"error": "unauthorized"}), 401

    _device_queue["should_reset"] = True

    print(f"\n--- /api/device/queue-reset: queued session reset ---")
    _log_call("queue-reset", {}, 200, "queued")

    return jsonify({"status": "queued"}), 200


@app.route("/api/device/register-token", methods=["POST"])
def register_token():
    """
    Called by the app whenever it gets a new FCM token (see FcmService.onNewToken).
    Expects JSON: { "token": "..." }
    """
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    if not token:
        return jsonify({"error": "Missing 'token'"}), 400

    _device_fcm_token["token"] = token
    print(f"\n--- /api/device/register-token: stored new token ---")
    print(f"  token (first 20 chars): {token[:20]}...")

    return jsonify({"status": "registered"}), 200


@app.route("/api/device/push-payment", methods=["POST"])
def push_payment():
    """
    Sends a push notification to the phone to trigger a payment, waking it
    even if backgrounded/locked/killed. Expects JSON:
      { "amount": "10", "currency": "EUR" }
    """
    if not _check_secret():
        return jsonify({"error": "unauthorized"}), 401

    if not _firebase_ready:
        return jsonify({"error": "Firebase not configured on this server"}), 500

    token = _device_fcm_token.get("token")
    if not token:
        return jsonify({"error": "No device has registered an FCM token yet"}), 400

    body = request.get_json(silent=True) or {}
    amount = body.get("amount", "5")
    currency = body.get("currency", "EUR")

    message = messaging.Message(
        data={
            "amount": str(amount),
            "currency": str(currency),
            "should_reset": "false",
        },
        token=token,
        android=messaging.AndroidConfig(priority="high"),
    )

    try:
        response = messaging.send(message)
        print(f"\n--- /api/device/push-payment: sent {amount} {currency} ---")
        print(f"  FCM message id: {response}")
        _log_call("push-payment", {"amount": amount, "currency": currency}, 200, response)
        return jsonify({"status": "sent", "message_id": response}), 200
    except Exception as e:
        print(f"  -> FCM send failed: {e}")
        return jsonify({"error": f"Failed to send push: {e}"}), 502


@app.route("/api/device/push-reset", methods=["POST"])
def push_reset():
    """Sends a push telling the phone to clear/re-establish its session."""
    if not _check_secret():
        return jsonify({"error": "unauthorized"}), 401

    if not _firebase_ready:
        return jsonify({"error": "Firebase not configured on this server"}), 500

    token = _device_fcm_token.get("token")
    if not token:
        return jsonify({"error": "No device has registered an FCM token yet"}), 400

    message = messaging.Message(
        data={"should_reset": "true"},
        token=token,
        android=messaging.AndroidConfig(priority="high"),
    )

    try:
        response = messaging.send(message)
        print(f"\n--- /api/device/push-reset: sent ---")
        _log_call("push-reset", {}, 200, response)
        return jsonify({"status": "sent", "message_id": response}), 200
    except Exception as e:
        print(f"  -> FCM send failed: {e}")
        return jsonify({"error": f"Failed to send push: {e}"}), 502


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
  input[type=text] { flex: 1; padding: 8px 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; }
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
</style>
</head>
<body>

<h1>Tap to Pay - backend test console</h1>
<div class="sub">
  Talks directly to this Flask backend. Note: this cannot trigger an actual
  NFC card tap -- that only happens inside the Android app on a physical
  phone. Use this to test the session-establishment call and inspect what
  Adyen's servers return.
</div>

<div class="card">
  <h2>Backend status</h2>
  <div id="status"><span class="muted">Not checked yet.</span></div>
  <button class="secondary" style="margin-top:10px;" onclick="loadStatus(1)">Check status</button>
</div>

<div class="card">
  <h2>Dashboard secret</h2>
  <div class="sub" style="margin-bottom:10px;">
    Only needed if you've set DASHBOARD_SECRET as an environment variable on
    the server. Stored in this browser tab only, never saved.
  </div>
  <div class="row">
    <input type="text" id="dashSecret" placeholder="x-dashboard-secret value (leave blank if not set)">
  </div>
</div>

<div class="card">
  <h2>Test /establish-session</h2>
  <div class="row">
    <input type="text" id="setupToken" placeholder="setupToken (from the Android app's authenticate() callback)">
  </div>
  <button onclick="testEstablish()">Send</button>
  <pre id="establishResult" style="display:none; margin-top:12px;"></pre>
</div>

<div class="card">
  <h2>Remote trigger (Web POS)</h2>
  <div class="sub" style="margin-bottom:10px;">
    Queues a command for the phone's next poll (~3s). The phone will wake
    and come to the foreground automatically -- only use this on a device
    you control and expect this on.
  </div>
  <div class="row">
    <input type="text" id="remoteAmount" placeholder="Amount" value="5" style="max-width:120px;">
    <input type="text" id="remoteCurrency" placeholder="Currency" value="EUR" style="max-width:100px;">
    <button onclick="queuePayment()">Send payment to device</button>
  </div>
  <button class="secondary" onclick="queueReset()">Force session reset</button>
  <pre id="remoteResult" style="display:none; margin-top:12px;"></pre>
</div>

<div class="card">
  <h2>Recent activity</h2>
  <button class="secondary" onclick="loadRecent()">Refresh</button>
  <div id="recentList" style="margin-top:12px;"></div>
</div>

<script>
async function loadStatus(attempt) {
  attempt = attempt || 1;
  const el = document.getElementById('status');
  if (attempt === 1) {
    el.innerHTML = '<span class="muted">Checking (may take up to a minute if the server was asleep)...</span>';
  }
  try {
    const health = await fetch('/health').then(r => r.json());
    const cfg = await fetch('/api/config-check').then(r => r.json());
    let html = '<span class="status-ok">Server running</span><br>';
    html += cfg.api_key_set
      ? '<span class="status-ok">API key configured</span><br>'
      : '<span class="status-bad">API key still a placeholder -- edit app.py</span><br>';
    html += cfg.merchant_account_set
      ? '<span class="status-ok">Merchant account: ' + cfg.merchant_account + '</span>'
      : '<span class="status-bad">Merchant account still a placeholder -- edit app.py</span>';
    el.innerHTML = html;
  } catch (e) {
    if (attempt < 6) {
      setTimeout(() => loadStatus(attempt + 1), 5000);
      el.innerHTML = '<span class="muted">Still waking up server, retrying (' + attempt + '/6)...</span>';
    } else {
      el.innerHTML = '<span class="status-bad">Cannot reach backend</span> <button class="secondary" onclick="loadStatus(1)">Retry</button>';
    }
  }
}

async function queuePayment() {
  const amount = document.getElementById('remoteAmount').value.trim();
  const currency = document.getElementById('remoteCurrency').value.trim();
  const secret = document.getElementById('dashSecret').value.trim();
  const out = document.getElementById('remoteResult');
  out.style.display = 'block';
  out.textContent = 'Sending...';
  try {
    const res = await fetch('/api/device/queue-payment', {
      method: 'POST',
      headers: {'content-type': 'application/json', 'x-dashboard-secret': secret},
      body: JSON.stringify({amount, currency})
    });
    const data = await res.json();
    out.textContent = 'Status: ' + res.status + '\\n\\n' + JSON.stringify(data, null, 2);
  } catch (e) {
    out.textContent = 'Request failed: ' + e;
  }
}

async function queueReset() {
  const secret = document.getElementById('dashSecret').value.trim();
  const out = document.getElementById('remoteResult');
  out.style.display = 'block';
  out.textContent = 'Sending...';
  try {
    const res = await fetch('/api/device/queue-reset', {
      method: 'POST',
      headers: {'content-type': 'application/json', 'x-dashboard-secret': secret}
    });
    const data = await res.json();
    out.textContent = 'Status: ' + res.status + '\\n\\n' + JSON.stringify(data, null, 2);
  } catch (e) {
    out.textContent = 'Request failed: ' + e;
  }
}

async function testEstablish() {
  const setupToken = document.getElementById('setupToken').value.trim();
  const secret = document.getElementById('dashSecret').value.trim();
  const out = document.getElementById('establishResult');
  out.style.display = 'block';
  out.textContent = 'Sending...';
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
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)  # hides the bare "POST ... 200 -" lines

    # host="0.0.0.0" so your phone (on the same LAN) can reach this machine.
    app.run(host="0.0.0.0", port=5000, debug=True)