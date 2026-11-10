# Deploying SWE Mock Platform to Render.com

This guide walks you through deploying **`swe-mock-platform`** on **Render.com** with full support for:
- ✅ **WebSockets** (for the real-time live coding room `/room/{id}`)
- ✅ **Persistent Disk** (so your SQLite `interviews.db` and `uploads/` survive redeploys)
- ✅ **Gmail SMTP** (for sending calendar `.ics` invites & breakdown threads)
- ✅ **Automatic HTTPS/SSL** (for Revolut Merchant API webhooks)

---

## Step 1: Prepare Your Code for Git

Before pushing to GitHub/GitLab, ensure confidential files are ignored:

1. Create or verify your `.gitignore` in the project root:
   ```gitignore
   .venv/
   __pycache__/
   *.pyc
   .DS_Store
   .pytest_cache/
   .env
   interviews.db
   interviews.db-wal
   interviews.db-shm
   uploads/email_outbox/
   credentials.json
   token.json
   ```

2. Initialize Git (if not already done) and push to GitHub:
   ```bash
   git init
   git add .
   git commit -m "Initial commit for Render deployment"
   # Push to your private GitHub repo:
   # git remote add origin git@github.com:yourusername/swe-mock-platform.git
   # git push -u origin main
   ```

---

## Step 2: Create a Web Service on Render

1. Go to your [Render Dashboard](https://dashboard.render.com/) and click **New +** &rarr; **Web Service**.
2. Connect your GitHub repository.
3. Configure the service settings:
   - **Name**: `swe-mock-platform` (or your choice)
   - **Region**: Frankfurt / Ireland (close to Europe/Rome for low latency)
   - **Branch**: `main`
   - **Runtime**: **Python 3**
   - **Build Command**:
     ```bash
     pip install --upgrade pip && pip install -r requirements.txt
     ```
   - **Start Command**:
     ```bash
     uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"
     ```
   - **Instance Type**: 
     - *Starter* ($7/mo) is recommended because it supports **Persistent Disks** (Free instances spin down and do not support persistent disks, which SQLite needs).

---

## Step 3: Add a Persistent Disk (Crucial for SQLite)

Because Render containers are recreated on every code deploy, you must attach a persistent disk so `interviews.db` and uploaded files aren't wiped:

1. In your Web Service settings on Render, scroll to the **Disks** section (or click **Disks** in the left sidebar).
2. Click **Add Disk**:
   - **Name**: `swe-data`
   - **Mount Path**: `/data`
   - **Size**: `1 GB` (plenty for SQLite and resumes)
3. Save the disk.

Now, update your database path to use `/data` via environment variables (Step 4).

---

## Step 4: Configure Environment Variables on Render

In your Render service dashboard, click **Environment** &rarr; **Add Environment Variable** and add the following:

| Key | Example Value | Notes |
| :--- | :--- | :--- |
| `BASE_URL` | `https://your-service.onrender.com` | Update to custom domain once set |
| `DATABASE_PATH` | `/data/interviews.db` | Points to your persistent disk |
| `HOST_TIMEZONE` | `Europe/Rome` | Timezone for availability schedules |
| `TRUST_PROXY_HEADERS` | `true` | Required for client IP tracking behind Render proxy |
| `ADMIN_PASSWORD` | `YourStrongAdminPassword!` | Password to access `/admin` |
| `ADMIN_SECRET` | *(64-char random string)* | Run `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ADMIN_EMAIL` | `coach@yourdomain.com` | Gets booking notification alerts |
| `PRICE_SINGLE_STANDARD_EUR` | `70` | Standard single session fee |
| `PRICE_BUNDLE3_STANDARD_EUR` | `190` | 3-mock bundle fee |
| `YOUTUBE_DISCOUNT_PERCENT` | `20` | Promotional discount percentage |
| `SESSION_DURATION_MINUTES`| `60` | Duration in minutes |
| `REVOLUT_PAYMENT_URL` | `https://revolut.me/yourusername` | Fallback Revolut link |
| `REVOLUT_API_KEY` | `sk_live_...` | From Revolut Business Merchant API |
| `REVOLUT_WEBHOOK_SECRET` | `wsk_live_...` | Generated when registering webhook |
| `REVOLUT_API_BASE` | `https://merchant.revolut.com` | Production API endpoint |
| `REVOLUT_API_VERSION` | `2024-09-01` | |
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` | `your-email@gmail.com` | Your Gmail address |
| `SMTP_APP_PASSWORD` | `xxxx xxxx xxxx xxxx` | 16-char Google App Password |
| `SMTP_DOMAIN` | `gmail.com` | Or your custom domain if using Google Workspace |

---

## Step 5: Configure Revolut Merchant Webhooks

To enable automatic booking confirmations:

1. Log in to your **Revolut Business / Revolut Pro** account.
2. Navigate to **Merchant** &rarr; **Developer** &rarr; **Webhooks**.
3. Click **Add Webhook**:
   - **URL**: `https://your-service.onrender.com/webhooks/revolut` *(or your custom domain)*
   - **Events**: Check `ORDER_COMPLETED`
4. Copy the **Signing Secret** provided by Revolut (`wsk_...`).
5. Paste it into the `REVOLUT_WEBHOOK_SECRET` variable in your Render dashboard and click **Save Changes**.

---

## Step 6: Verify Your Deployment

Once Render finishes deploying:

1. **Check Health**: Open `https://your-service.onrender.com/` — the landing page should render with open booking slots.
2. **Check Admin**: Log in at `https://your-service.onrender.com/admin` with your `ADMIN_PASSWORD`.
3. **Verify WebSockets**:
   - Create a test booking on `/book`.
   - Open the live interview room `/room/{session_id}`.
   - Look at the top bar: the dot must turn green with **"Live Sync Connected"**.
4. **Verify Email**: Approve a session in `/admin` (or trigger a test email) and verify the calendar invite lands in your email inbox.

---

## (Optional) Custom Domain

To use `interview.yourdomain.com`:
1. In Render, go to **Settings** &rarr; **Custom Domains** &rarr; **Add Custom Domain**.
2. Add a `CNAME` record in your DNS provider (Cloudflare, Namecheap, GoDaddy, etc.) pointing to your Render `.onrender.com` URL.
3. Update `BASE_URL` in your Render Environment settings to `https://interview.yourdomain.com`.
4. Update the Revolut Webhook URL to `https://interview.yourdomain.com/webhooks/revolut`.
