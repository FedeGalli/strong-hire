# Self-Hosting SWE Mock Platform on Raspberry Pi (with Cloudflare Tunnel)

This guide walks you through deploying **`swe-mock-platform`** 24/7 on a **Raspberry Pi** at home, even behind a home router with **no public IP** (CGNAT) and **no port forwarding**.

---

## Why Raspberry Pi + Cloudflare Tunnel is the Perfect Setup

- ⚡ **Extremely Low Power**: Consumes only ~3–5 Watts (costs ~$0.50/month in electricity).
- 🔒 **No Port Forwarding**: Your home router firewall stays 100% closed.
- 💾 **Native SQLite**: `interviews.db` lives directly on the Pi's storage.
- 🌐 **Free Custom Domain & SSL**: Cloudflare handles HTTPS certificates and DDoS protection.
- 🔄 **WebSockets & Webhooks**: Live interview room and Revolut webhooks work seamlessly.

---

## Architecture

```
[Candidate / Revolut Webhook]
             │ (HTTPS)
             ▼
    [Cloudflare Edge]
             │ (Encrypted Tunnel over outbound port 443 / QUIC)
             ▼
      [Raspberry Pi]
   ├── cloudflared (systemd service)
   │        │ (localhost:8000)
   └── swe-mock (systemd service: Uvicorn + FastAPI)
            │
       interviews.db (SQLite on SD/SSD)
```

---

## Step 1: Prepare the Raspberry Pi

SSH into your Raspberry Pi:
```bash
ssh pi@raspberrypi.local
# (or your Pi's local IP address)
```

Update packages and install dependencies:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git curl
```

---

## Step 2: Clone & Set Up the Application

1. Clone your project:
   ```bash
   cd ~
   git clone <YOUR_GIT_REPO_URL> swe-mock-platform
   cd swe-mock-platform
   ```

2. Create virtual environment and install requirements:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. Configure your production `.env`:
   ```bash
   cp .env.example .env
   nano .env
   ```
   Set your production values:
   ```ini
   BASE_URL="https://mock.yourdomain.com"
   TRUST_PROXY_HEADERS="true"
   HOST_TIMEZONE="Europe/Rome"
   ADMIN_PASSWORD="YourStrongPasswordHere"
   ADMIN_SECRET="GenerateRandomSecret64Chars"
   ADMIN_EMAIL="your-email@gmail.com"

   # Revolut Merchant API
   REVOLUT_API_KEY="sk_live_..."
   REVOLUT_WEBHOOK_SECRET="wsk_live_..."
   REVOLUT_API_BASE="https://merchant.revolut.com"

   # Gmail SMTP
   SMTP_HOST="smtp.gmail.com"
   SMTP_PORT=587
   SMTP_USER="your-email@gmail.com"
   SMTP_APP_PASSWORD="your-16-char-app-password"
   SMTP_DOMAIN="gmail.com"
   ```

4. Initialize the clean database:
   ```bash
   python3 -c "from app.database import init_db; init_db()"
   ```

---

## Step 3: Run the App as a Systemd Service (Auto-Start on Boot)

Create `/etc/systemd/system/swe-mock.service`:
```bash
sudo nano /etc/systemd/system/swe-mock.service
```

Paste the following configuration (replace `pi` with your Pi username if different):
```ini
[Unit]
Description=SWE Mock Interview Platform
After=network.target

[Service]
User=pi
Group=pi
WorkingDirectory=/home/pi/swe-mock-platform
EnvironmentFile=/home/pi/swe-mock-platform/.env
ExecStart=/home/pi/swe-mock-platform/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable swe-mock
sudo systemctl start swe-mock

# Check that it's running:
sudo systemctl status swe-mock
```

Test it locally on the Pi:
```bash
curl http://localhost:8000/
# Should return the landing page HTML
```

---

## Step 4: Install & Configure Cloudflare Tunnel

### 4.1 Install `cloudflared` on Raspberry Pi

Detect your architecture (ARM64 for Pi 4/5 or 64-bit OS; ARMv7 for 32-bit):
```bash
dpkg --print-architecture
```

Install the appropriate Debian package:

**For 64-bit (arm64 - Raspberry Pi 4 / 5):**
```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared.deb
rm cloudflared.deb
```

**For 32-bit (armhf):**
```bash
curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm.deb
sudo dpkg -i cloudflared.deb
rm cloudflared.deb
```

Verify installation:
```bash
cloudflared --version
```

---

### 4.2 Authenticate with Cloudflare

1. Run:
   ```bash
   cloudflared tunnel login
   ```
2. It will output a URL. Open that URL on your computer's browser and select your domain (free Cloudflare account).
3. The auth cert will be saved to `~/.cloudflared/cert.pem`.

---

### 4.3 Create the Tunnel & Route DNS

1. Create a named tunnel:
   ```bash
   cloudflared tunnel create pi-mock
   ```
   Note the generated **Tunnel ID** (UUID).

2. Route your subdomain (e.g. `mock.yourdomain.com`) to the tunnel:
   ```bash
   cloudflared tunnel route dns pi-mock mock.yourdomain.com
   ```

3. Create the configuration file:
   ```bash
   nano ~/.cloudflared/config.yml
   ```
   Paste the following (replace `<TUNNEL_ID>` and your username):
   ```yaml
   tunnel: pi-mock
   credentials-file: /home/pi/.cloudflared/<TUNNEL_ID>.json

   ingress:
     - hostname: mock.yourdomain.com
       service: http://localhost:8000
     - service: http_status:404
   ```

---

### 4.4 Install `cloudflared` as a System Service (Auto-Start on Boot)

Run:
```bash
sudo cloudflared --config /home/pi/.cloudflared/config.yml service install
sudo systemctl start cloudflared
sudo systemctl status cloudflared
```

---

## Step 5: Configure Revolut Webhook

1. Open your **Revolut Business / Pro** dashboard &rarr; **Merchant** &rarr; **Webhooks**.
2. Add Webhook:
   - **URL**: `https://mock.yourdomain.com/webhooks/revolut`
   - **Events**: `ORDER_COMPLETED`
3. Copy the signing secret (`wsk_...`) and ensure it matches `REVOLUT_WEBHOOK_SECRET` in `/home/pi/swe-mock-platform/.env`.
4. If you updated `.env`, restart the app service:
   ```bash
   sudo systemctl restart swe-mock
   ```

---

## Step 6: Backup Recommendations for SQLite on SD Card

Raspberry Pi SD cards can wear out over years of frequent writes. To ensure your `interviews.db` is safe:

1. **Option A: Run Pi from a USB SSD** instead of a micro-SD card (fastest, most durable).
2. **Option B: Daily automated SQLite backup** using cron:
   ```bash
   crontab -e
   ```
   Add a line to back up the database every night at 3:00 AM:
   ```cron
   0 3 * * * sqlite3 /home/pi/swe-mock-platform/interviews.db ".backup '/home/pi/backups/interviews_$(date +\%F).db'"
   ```

---

## Summary of Useful Commands

| Task | Command |
| :--- | :--- |
| **Check App Status** | `sudo systemctl status swe-mock` |
| **View Live App Logs** | `sudo journalctl -u swe-mock -f` |
| **Restart App** | `sudo systemctl restart swe-mock` |
| **Check Tunnel Status** | `sudo systemctl status cloudflared` |
| **View Tunnel Logs** | `sudo journalctl -u cloudflared -f` |
