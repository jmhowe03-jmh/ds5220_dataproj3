# DS5220 Data Project 3 — Part 1 Deployment Guide

This guide walks through every step to deploy the Part 1 ingestion pipeline
(EventBridge → Lambda → DynamoDB + S3) using Chalice.

> **Lambda file-size note:** AWS limits zipped deployment packages to **50 MB**.
> Pandas and Matplotlib alone are ~60 MB. This project avoids the limit by
> using two public **Lambda Layers** that supply those libraries — your
> deployment package only bundles `requests` (~300 KB total). No changes
> are needed to stay under the limit as long as you don't add new heavy
> packages to `requirements.txt`.

---

## Prerequisites

Make sure you have these installed and configured **on your local machine**
before starting. Run each check command; if it fails, install the tool first.

```bash
# 1. Python 3.9  (must match the Lambda layers)
python3.9 --version

# 2. AWS CLI — configure with your IAM credentials
aws --version
aws configure          # enter Access Key, Secret, region=us-east-1, format=json

# 3. Chalice
pip install chalice    # or: pip3 install chalice
chalice --version      # should show 1.29+ or similar
```

---

## Step 1 — Choose Your S3 Bucket Name

Pick a **globally unique** bucket name (e.g. `dp3-weather-jh`).
You'll use this name in Steps 2 and 4.

---

## Step 2 — Create the DynamoDB Table

Open the AWS Console → **DynamoDB → Tables → Create table**.

| Setting | Value |
|---------|-------|
| Table name | `SyracuseWeather` |
| Partition key | `city` — type **String** |
| Sort key | `timestamp` — type **Number** |
| Table class | DynamoDB Standard (default) |
| Capacity | On-demand (default, free tier friendly) |

Click **Create table** and wait until the status shows **Active**.

---

## Step 3 — Create the S3 Bucket (with public read access for the plot)

AWS blocks public access by default on new accounts. Follow these steps carefully.

### 3a — Create the bucket

1. Console → **S3 → Create bucket**
2. Bucket name: `dp3-weather-jh` (or your chosen name from Step 1)
3. Region: `us-east-1`
4. **Block Public Access settings**: uncheck **"Block all public access"**
   and confirm the warning checkbox.
5. Leave everything else at defaults → **Create bucket**.

### 3b — Add a bucket policy for public read

After the bucket is created:

1. Click into the bucket → **Permissions** tab → **Bucket policy → Edit**
2. Paste this policy (replace `dp3-weather-jh` with your bucket name):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadPlot",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::dp3-weather-jh/weather/*"
    }
  ]
}
```

3. Click **Save changes**. The plot URL returned by `/plot` will now be
   publicly accessible.

---

## Step 4 — Update Config Files With Your Bucket Name

Open `.chalice/config.json` and replace `dp3-weather-jh` with your actual
bucket name in two places:

```json
"environment_variables": {
    "S3_BUCKET": "YOUR-BUCKET-NAME-HERE",   ← change this
    ...
}
```

Then open `.chalice/policy-dev.json` and do the same:

```json
"Resource": "arn:aws:s3:::YOUR-BUCKET-NAME-HERE/*"   ← change this
```

Also update the default fallback in `app.py` line 47:

```python
S3_BUCKET = os.environ.get("S3_BUCKET", "YOUR-BUCKET-NAME-HERE")
```

---

## Step 5 — Install Chalice Dependencies Locally

From the project root (the folder containing `app.py`):

```bash
cd path/to/dataproj3

# Create and activate a Python 3.9 virtual environment
python3.9 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# Install chalice and requests (the only package in requirements.txt)
pip install chalice requests
```

---

## Step 6 — Deploy with Chalice

Make sure your virtual environment is active and you're in the project root.

```bash
chalice deploy
```

Chalice will:
1. Package `app.py` + `requests` (~300 KB — well under the 50 MB limit)
2. Create two Lambda functions:
   - `weather-system-dev` — the REST API (API Gateway + Lambda)
   - `weather-system-dev-ingest_weather` — the scheduled ingestion Lambda
3. Create an EventBridge rule that fires the ingestion Lambda every hour
4. Print a **REST API URL** at the end — save this!

Expected output:
```
Creating deployment package.
Updating policy for IAM role: weather-system-dev
Creating Rest API
Resources deployed:
  - Lambda ARN: arn:aws:lambda:us-east-1:...
  - Rest API URL: https://XXXXXXXX.execute-api.us-east-1.amazonaws.com/api/
```

---

## Step 7 — Test Your Endpoints

Replace `<your-api-url>` with the URL printed by `chalice deploy`.

```bash
# Zone apex — should return about + resources
curl https://<your-api-url>/

# Most recent weather reading
curl https://<your-api-url>/current

# Trend summary
curl https://<your-api-url>/trend

# Plot URL (image is only generated after the first ingestion run)
curl https://<your-api-url>/plot
```

Expected responses:

```json
// GET /
{
  "about": "Tracks hourly weather in Syracuse, NY...",
  "resources": ["current", "trend", "plot"]
}

// GET /current
{
  "response": "Syracuse, NY (2026-05-05 18:00 UTC): 58.3°F | Humidity 62% | Wind 9.2 mph"
}

// GET /trend
{
  "response": "Syracuse weather over 12 hourly readings: avg 57.4°F (range 52.1–63.8°F)..."
}

// GET /plot
{
  "response": "https://dp3-weather-jh.s3.us-east-1.amazonaws.com/weather/syracuse-latest.png"
}
```

---

## Step 8 — Trigger the First Ingestion Manually

The ingestion Lambda fires on the hour, but you can trigger it right away
via the AWS Console to verify everything works before waiting:

1. Console → **Lambda → weather-system-dev-ingest_weather**
2. Click **Test** → create a new test event (leave the JSON as `{}`)
3. Click **Test** again → check the **Execution results** and **CloudWatch logs**

You should see log lines like:
```
Open-Meteo OK | temp=58.30°F | humidity=62.0% | wind=9.2 mph
DynamoDB write OK | city=Syracuse | timestamp=1746460800
Plot generated | size=142304 bytes
Plot uploaded | url=https://dp3-weather-jh.s3.us-east-1.amazonaws.com/...
```

After this first run, `/current` and `/plot` will return real data.

---

## Step 9 — Register With the Discord Bot

Once your API is live and `/` returns the correct JSON shape:

In the course `#dp3` Discord channel:

```
/register <your-project-id> <your-username> https://<your-api-url>
```

Then test it:

```
/project <your-project-id>
/project <your-project-id> current
/project <your-project-id> plot
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `chalice deploy` fails with `Invalid layer ARN` | You're in the wrong region | Make sure `aws configure` has region `us-east-1` |
| `/current` returns "No data yet" | Ingestion hasn't run | Manually trigger the Lambda (Step 8) |
| Plot URL returns 403 | Bucket policy missing or wrong | Re-check Step 3b |
| `AccessDeniedException` in CloudWatch | IAM policy not applied | Re-run `chalice deploy`; check `policy-dev.json` has the right bucket/table names |
| Lambda timeout | Memory/time too low | Already set to 512 MB / 120 s in `config.json` |

---

## File Summary

| File | What it does |
|------|-------------|
| `app.py` | All code: ingestion schedule + 3 API routes |
| `requirements.txt` | Only `requests` (heavy libs come from layers) |
| `.chalice/config.json` | Lambda timeout, memory, env vars, layer ARNs |
| `.chalice/policy-dev.json` | IAM permissions for DynamoDB + S3 |
