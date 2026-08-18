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
  Talks directly to this Flask backend. 
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
    the server.
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
async function loadStatus(attempt) {
  attempt = attempt || 1;
  const el = document.getElementById('status');
  if (attempt === 1) {
    el.innerHTML = '<span class="muted">Checking...</span>';
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
    el.innerHTML = '<span class="status-bad">Cannot reach backend</span>';
  }
}

// ------------------------------------------------------------------
// CHANGED: Now calls /push-payment instead of /queue-payment
// ------------------------------------------------------------------
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
}

// ------------------------------------------------------------------
// CHANGED: Now calls /push-reset instead of /queue-reset
// ------------------------------------------------------------------
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