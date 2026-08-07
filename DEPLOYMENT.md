# Deploying LLM Gateway to Render.com

This guide provides step-by-step instructions for deploying your FastAPI LLM Gateway project to [Render.com](https://render.com) using the generated Blueprints configuration.

---

## 🛠️ Prerequisites

1. A **GitHub** account and `git` installed locally.
2. A **Render** account (linked to GitHub for easy deployments).
3. A real **Google Gemini API Key** (from Google AI Studio).

---

## 🚀 Step-by-Step Deployment

### Step 1: Push Project to GitHub

1. Initialize git (if not already done) and commit the changes:
   ```bash
   git init
   git add .
   git commit -m "Configure project for Render deployment"
   ```
2. Create a repository on GitHub (e.g. `llm-gateway`).
3. Link your local repo to GitHub and push:
   ```bash
   git remote add origin https://github.com/your-username/llm-gateway.git
   git branch -M main
   git push -u origin main
   ```

### Step 2: Deploy Blueprint on Render

1. Log in to the **Render Dashboard** and click **New** (top right) -> **Blueprint**.
2. Select your `llm-gateway` GitHub repository.
3. Render will parse your `render.yaml` file and prompt you to name the Blueprint Group (e.g. `llm-gateway-stack`).
4. Enter the required **Environment Variables**:
   *   `GEMINI_API_KEY`: Input your real Gemini API key (`AIzaSy...`).
   *   `ADMIN_API_KEY`: Provide a secure token for administrative endpoints (e.g., `sk-admin-demo-key`).
5. Click **Apply**.
6. Render will spin up two services:
   *   `redis-cache`: A managed Key Value (Redis-compatible) instance.
   *   `llm-gateway`: A Docker-based Web Service exposing the gateway on port `8000`.

### Step 3: Monitor & Wake Up (Free Tier Behavior)

> [!WARNING]
> **Free Tier Sleep Behavior:** Render's Free tier web service will sleep (spin down to 0 replicas) after **15 minutes of inactivity**. The next incoming request will automatically wake the service, but it will suffer a "cold start" delay of 30–60 seconds before responding.
>
> **Production Recommendation:** For production or "always-on" behavior, upgrade the Web Service plan in `render.yaml` from `free` to `starter` ($7/month). The Redis `keyvalue` instance remains free (25MB RAM, non-persistent) or can be upgraded to the `starter` tier ($10/month) for persistent storage.

---

## 🧪 Post-Deployment Test Commands

Once your service status says **"Live"**, copy the service's default URL from the dashboard (e.g. `https://llm-gateway-xxxx.onrender.com`).

### 1. Verify Gateway Health Check
This endpoint returns overall service health and downstream model connectivity:

**curl (Linux/macOS):**
```bash
curl -i https://your-app-name.onrender.com/health
```

**PowerShell (Windows):**
```powershell
Invoke-RestMethod -Uri "https://your-app-name.onrender.com/health" -Method Get
```

---

### 2. Standard (Non-Streaming) Chat Completion
Send a chat completion query to the real Gemini API through the gateway using the pre-seeded `team-a` key:

**curl (Linux/macOS):**
```bash
curl -s -X POST https://your-app-name.onrender.com/v1/chat/completions \
  -H "Authorization: Bearer sk-team-a-demo-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.5-flash",
    "messages": [
      {"role": "user", "content": "Explain quantum computing in one sentence."}
    ]
  }' | python3 -m json.tool
```

**PowerShell (Windows):**
```powershell
$headers = @{
    "Authorization" = "Bearer sk-team-a-demo-key"
    "Content-Type"  = "application/json"
}
$body = @{
    model = "gemini-3.5-flash"
    messages = @(
        @{ role = "user"; content = "Explain quantum computing in one sentence." }
    )
} | ConvertTo-Json -Depth 5

$response = Invoke-RestMethod -Uri "https://your-app-name.onrender.com/v1/chat/completions" -Method Post -Headers $headers -Body $body
$response | ConvertTo-Json -Depth 5
```

---

### 3. Server-Sent Events (SSE) Streaming
Verify response streaming through the gateway:

**curl (Linux/macOS):**
```bash
curl -N -X POST https://your-app-name.onrender.com/v1/chat/completions \
  -H "Authorization: Bearer sk-team-a-demo-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.5-flash",
    "messages": [
      {"role": "user", "content": "Write a 5-step checklist for coding."}
    ],
    "stream": true
  }'
```

**PowerShell (Windows):**
```powershell
$headers = @{
    "Authorization" = "Bearer sk-team-a-demo-key"
    "Content-Type"  = "application/json"
}
$body = @{
    model = "gemini-3.5-flash"
    messages = @(
        @{ role = "user"; content = "Write a 5-step checklist for coding." }
    )
    stream = $true
} | ConvertTo-Json -Depth 5

# PowerShell Core/7 supports -SkipHttpErrorCheck or reading chunked content:
Invoke-WebRequest -Uri "https://your-app-name.onrender.com/v1/chat/completions" -Method Post -Headers $headers -Body $body -OutFile "stream_test.txt"
Get-Content "stream_test.txt"
```

---

### 4. Verify Rate Limiting
Test the rate-limiting functionality using `team-b`, which has a strict limit of 5 requests per minute (RPM):

**curl (Linux/macOS):**
```bash
# Fire 7 fast requests; you will see HTTP 429 errors starting from the 6th call
for i in {1..7}; do
  curl -w "\nHTTP Code: %{http_code}\n" -s -o /dev/null -X POST https://your-app-name.onrender.com/v1/chat/completions \
    -H "Authorization: Bearer sk-team-b-demo-key" \
    -H "Content-Type: application/json" \
    -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}'
done
```

**PowerShell (Windows):**
```powershell
# Fire 7 fast requests in sequence
for ($i=1; $i -le 7; $i++) {
    $body = @{ model = "gpt-4o-mini"; messages = @(@{ role = "user"; content = "hi" }) } | ConvertTo-Json
    try {
        $resp = Invoke-WebRequest -Uri "https://your-app-name.onrender.com/v1/chat/completions" -Method Post -Headers @{ "Authorization" = "Bearer sk-team-b-demo-key"; "Content-Type" = "application/json" } -Body $body -UseBasicParsing
        Write-Host "Request $i: HTTP $($resp.StatusCode)"
    } catch {
        Write-Host "Request $i: HTTP $($_.Exception.Response.StatusCode.value__)"
    }
}
```

---

### 5. Admin API Overrides
Retrieve team details or dynamically adjust properties without server reboots:

**curl (Linux/macOS):**
```bash
# Retrieve active team statuses
curl -s https://your-app-name.onrender.com/admin/teams -H "X-Admin-Key: sk-admin-demo-key" | python3 -m json.tool

# Dynamically override rate limits for team-b to 100 RPM
curl -s -X PATCH https://your-app-name.onrender.com/admin/teams/team-b/limits \
  -H "X-Admin-Key: sk-admin-demo-key" \
  -H "Content-Type: application/json" \
  -d '{"rate_limit": {"rpm": 100, "tpm": 100000}}' | python3 -m json.tool
```

**PowerShell (Windows):**
```powershell
$adminHeaders = @{
    "X-Admin-Key" = "sk-admin-demo-key"
}
# Get Teams
Invoke-RestMethod -Uri "https://your-app-name.onrender.com/admin/teams" -Headers $adminHeaders

# Patch Limits
$patchBody = @{
    rate_limit = @{ rpm = 100; tpm = 100000 }
} | ConvertTo-Json
Invoke-RestMethod -Uri "https://your-app-name.onrender.com/admin/teams/team-b/limits" -Method Patch -Headers @{ "X-Admin-Key" = "sk-admin-demo-key"; "Content-Type" = "application/json" } -Body $patchBody
```

---

## 🔍 Troubleshooting

### 1. Redis Connection Failures
*   **Symptom:** Logs show `ConnectionError: Error connecting to Redis` or SSL issues.
*   **Resolution:** Render's internal database URLs occasionally use the `rediss://` protocol (secure TLS). The application's configuration loader `app/config.py` has been updated to parse `rediss://` strings and append `ssl_cert_reqs=none` automatically. Ensure you copy the **Internal Redis Connection String** (starts with `redis://` or `rediss://`) from the Render panel to your Blueprint or environment settings.

### 2. Health Checks Fails / Service Unhealthy
*   **Symptom:** Render cancels deployment with "Health Check failed".
*   **Resolution:** Verify the endpoint path matches `/health` in `render.yaml`. Check application logs using `docker compose logs` locally to verify if initialization failed (e.g. invalid API key format, missing environment variable, or syntax error).

### 3. Docker Compile/Build Failure
*   **Symptom:** Pipeline fails during the build step.
*   **Resolution:** Render builds Docker containers from your root `Dockerfile`. Check the build logs to see if a package in `requirements.txt` failed to compile. Standard `python:3.11-slim` handles the gateway packages smoothly.
