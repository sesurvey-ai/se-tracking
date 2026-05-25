# Dokploy Deployment Guide

Deploy 3 services to Dokploy on the existing Hostinger VPS (`srv1497632.hstgr.cloud`,
where `se-key` and `se-billing` already run).

## Services Overview

| Service | Port | DB volume | Domain (suggested) |
|---------|------|-----------|--------------------|
| **emcs-api** | 5500 | `emcs-data` | `emcs.sesurvey.cloud` |
| **debt-api** | 5600 | `debt-data` | `debt.sesurvey.cloud` |
| **se-tracking** | 5400 | `tracking-data` | `tracking.sesurvey.cloud` |

All three live on the same Docker network so se-tracking can call the APIs
internally (`http://emcs-api:5500`, `http://debt-api:5600`) without going
through the public internet.

## Steps

### 1. Push the 3 repos to Git

Each folder needs its own Git repo (or use a monorepo). Push to GitHub /
GitLab. Example:

```bash
# C:\Users\i9\Desktop\emcs-api
git init && git add . && git commit -m "initial" && git remote add origin <url> && git push -u origin main

# Same for C:\Users\i9\Desktop\debt-api
# Same for C:\Users\i9\Desktop\se-tracking
```

### 2. In Dokploy panel for each service

For **emcs-api**:
- New Application → Git → repo URL → branch `main`
- Build type: **Dockerfile**
- Port: `5500`
- Domain: `emcs.sesurvey.cloud` (Traefik handles SSL)
- Environment variables:
  ```
  EMCS_API_KEY=<copy from local .env>
  EMCS_DB_PATH=/data/emcs.db
  ```
- Volume mount: `/data` → persistent volume named `emcs-data`
- Deploy

For **debt-api**: same pattern, port `5600`, env `DEBT_API_KEY`, volume `debt-data`.

For **se-tracking**: port `5400`, set:
```
TRACKING_API_KEY=<generate strong random>
SE_KEY_URL=https://key.sesurvey.cloud
SE_KEY_API_KEY=<same as before>
SE_BILLING_URL=https://billing.sesurvey.cloud
SE_BILLING_TOKEN=<same as before>
EMCS_API_URL=http://emcs-api:5500          # internal docker network
EMCS_API_KEY=<same as emcs-api>
DEBT_API_URL=http://debt-api:5600          # internal docker network
DEBT_API_KEY=<same as debt-api>
ISURVEY_API_USERNAME=<iSurvey login>
ISURVEY_API_PASSWORD=<iSurvey password>
ISURVEY_INITIAL_FROM=2023-01-01
ISURVEY_API_INTERVAL_MIN=60
SYNC_INTERVAL_MIN=5
TRACKING_DB_PATH=/data/tracking.db
```
- Volume: `/data` → `tracking-data`
- Domain: `tracking.sesurvey.cloud`

### 3. DNS records

Add A records at your DNS provider:
```
emcs.sesurvey.cloud      A    187.127.96.172
debt.sesurvey.cloud      A    187.127.96.172
tracking.sesurvey.cloud  A    187.127.96.172
```

### 4. Migrate existing data

After services come up (with empty DBs), migrate local data via API:

```bash
# emcs-api
python -c "
import sqlite3, requests
src = sqlite3.connect(r'D:\\trackingDB\\emcs.db')
src.row_factory = sqlite3.Row
records = [dict(r) for r in src.execute('SELECT claim_no,survey_no,invoice_no,invoice_seq,date_approve,offer_amount,approve_amount,deduct_amount,deduct_reason,claim_type,surveyer,acc_province,source_file,source_sheet FROM pw_records')]
requests.post('https://emcs.sesurvey.cloud/api/records/bulk',
    headers={'X-API-Key': '<EMCS_API_KEY>'}, json={'records': records}, timeout=120).json()
"

# debt-api — same but read debt_records and chunk in 5000s
```

### 5. Hpw.py + se-tracking config

Update `C:\Users\i9\Desktop\pw\.env`:
```
EMCS_API_URL=https://emcs.sesurvey.cloud
EMCS_API_KEY=<key>
```

`se-tracking/.env` can stay local-pointing OR also use the public URLs once
deployed. After deploy, the local `se-tracking` becomes redundant.

### 6. Verify

```bash
curl https://emcs.sesurvey.cloud/healthz
curl https://debt.sesurvey.cloud/healthz
curl https://tracking.sesurvey.cloud/healthz
```

Then open `https://tracking.sesurvey.cloud/` in a browser.

## Internal Docker network

In Dokploy, services that share a project use the same Docker network and
can resolve each other by container name. **se-tracking** calls
`http://emcs-api:5500` and `http://debt-api:5600` directly — no public DNS
required for inter-service traffic.

## Common gotchas

| Issue | Fix |
|-------|-----|
| `502 Bad Gateway` from Traefik | Service hasn't bound to its EXPOSE port yet — check logs |
| Volume losing data on redeploy | Make sure `/data` is a named volume in Dokploy UI |
| `unauthorized` from emcs/debt | `EMCS_API_KEY` mismatch between se-tracking and the API service |
| Hpw.py fails to POST | Verify `EMCS_API_URL` in `pw/.env` matches the deployed URL |
