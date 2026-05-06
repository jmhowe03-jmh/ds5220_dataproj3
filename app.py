"""
app.py — DS5220 Data Project 3
Syracuse, NY hourly weather tracker.

Part 1: @app.schedule  → ingests weather from Open-Meteo → DynamoDB + S3 plot
Part 2: @app.route     → exposes /  /current  /trend  /plot  via API Gateway

Environment variables (set in .chalice/config.json):
    S3_BUCKET   — name of the S3 bucket for plot images
    TABLE_NAME  — DynamoDB table name  (weather-data)
"""

import io
import logging
import os
import time
import math
import time as _time
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import matplotlib
matplotlib.use("Agg")          # non-interactive backend — required in Lambda
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests
from boto3.dynamodb.conditions import Key
from chalice import Chalice, Rate

# ---------------------------------------------------------------------------
# Logging — CloudWatch picks this up automatically
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Chalice app
# ---------------------------------------------------------------------------
app = Chalice(app_name="weather-system")

# ---------------------------------------------------------------------------
# Config — driven by Lambda environment variables (set in .chalice/config.json)
# ---------------------------------------------------------------------------
S3_BUCKET  = os.environ.get("S3_BUCKET",  "dp3-weather-plots-vxx4kn")   
TABLE_NAME = os.environ.get("TABLE_NAME", "weather-data")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

LAT      = 43.0481        # Syracuse, NY
LON      = -76.1474
CITY     = "Syracuse"
PLOT_KEY = "weather/syracuse-latest.png"   # fixed key → stable public URL


# ===========================================================================
# PART 1 — Scheduled ingestion  (fires every hour via EventBridge)
# ===========================================================================

@app.schedule(Rate(1, unit=Rate.HOURS))
def ingest_weather(event):
    """
    Fetch current weather from Open-Meteo and write a timestamped record to DynamoDB. Then rebuild the plot and push
    it to S3 at a fixed key so /plot always returns a fresh image URL.
    """
    log.info("ingest_weather triggered | lat=%.4f lon=%.4f", LAT, LON)

    # --- 1. Fetch from Open-Meteo ----------------------------------------
    url = ("https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
        "&temperature_unit=fahrenheit"
        "&wind_speed_unit=mph"
        "&timezone=auto")
    try:
        log.info("Calling Open-Meteo API")
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data    = resp.json()
        current = data["current"]
        log.info(
            "Open-Meteo OK | temp=%.2f°F | humidity=%.1f%% | wind=%.1f mph | wmo=%s",
            current["temperature_2m"],
            current["relative_humidity_2m"],
            current["wind_speed_10m"],
            current.get("weather_code"),
        )
    except requests.RequestException as exc:
        log.exception("Open-Meteo fetch failed: %s", exc)
        return {"statusCode": 500, "error": str(exc)}

    # --- 2. Build DynamoDB item -------------------------------------------
    # DynamoDB rejects Python float — use Decimal(str(...)) to convert safely
    # timestamp stored as String to match table schema (sort key type = String)
    ts     = int(time.time())
    ts_str = str(ts)
    item = {
        "location_id": CITY,                                                         #partition key (String)
        "timestamp":   ts_str,                                                       #sort key (String)
        "temp_f":      Decimal(str(round(float(current["temperature_2m"]),       2))), #lots of wrangling lol
        "humidity":    Decimal(str(round(float(current["relative_humidity_2m"]), 1))),
        "wind_mph":    Decimal(str(round(float(current["wind_speed_10m"]),       1))),
        "wmo_code":    int(current.get("weather_code", 0)),
        "utc_time":    datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    log.info("DynamoDB item: %s", item)

    # --- 3. Write to DynamoDB --------------------------------------------
    try:
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        table    = dynamodb.Table(TABLE_NAME)
        table.put_item(Item=item)
        log.info("DynamoDB write OK | location_id=%s | timestamp=%d", CITY, ts)
    except Exception as exc:
        log.exception("DynamoDB write failed: %s", exc)
        return {"statusCode": 500, "error": str(exc)}

    # --- 4. Fetch history and regenerate plot ----------------------------
    # Plot failure must NOT crash the ingestion — log and continue
    try:
        history = _query_all(table)
        if len(history) >= 2:
            plot_buf = _generate_plot(history)
            if plot_buf:
                _upload_plot(plot_buf)
        else:
            log.info("Not enough history to plot yet (%d record(s))", len(history))
    except Exception as exc:
        log.exception("Plot generation/upload failed (non-fatal): %s", exc)

    log.info("ingest_weather complete | timestamp=%s", ts_str)
    return {
        "statusCode":  200,
        "location_id": CITY,
        "timestamp":   ts_str,
        "temp_f":      float(item["temp_f"]),    # cast Decimal → float for JSON
        "humidity":    float(item["humidity"]),
        "wind_mph":    float(item["wind_mph"]),
    }


# ===========================================================================
# Shared helpers
# ===========================================================================

def _get_table():
    """Return a boto3 DynamoDB Table resource."""
    try:
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        return dynamodb.Table(TABLE_NAME)
    except Exception as exc:
        log.exception("Failed to connect to DynamoDB: %s", exc)
        raise


def _query_all(table) -> list:
    """
    Return ALL records for CITY, sorted oldest-first.
    Handles DynamoDB pagination so the full history is returned
    regardless of table size.
    """
    log.info("Querying DynamoDB | location_id=%s", CITY)
    try:
        items  = []
        kwargs = dict(
            KeyConditionExpression=Key("location_id").eq(CITY),
            ScanIndexForward=True,
        )
        page = 0
        while True:
            resp  = table.query(**kwargs)
            batch = resp.get("Items", [])
            items.extend(batch)
            page += 1
            log.info("DynamoDB page %d: %d records (total=%d)", page, len(batch), len(items))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        log.info("DynamoDB query complete | total records=%d", len(items))
        return items
    except Exception as exc:
        log.exception("DynamoDB query failed: %s", exc)
        return []


def _generate_plot(items: list):
    """
    Build a dual-axis temperature + humidity line chart from DynamoDB records.
    Uses plain Python lists + datetime — no pandas required.
    Returns a PNG BytesIO buffer, or None if there's not enough data.
    """
    if len(items) < 2:
        log.info("Skipping plot — only %d record(s) available", len(items))
        return None

    log.info("Generating plot for %d data points", len(items))
    try:
        # Sort oldest-first and extract values
        sorted_items = sorted(items, key=lambda x: int(x["timestamp"]))
        datetimes = [
            datetime.fromtimestamp(int(i["timestamp"]), tz=timezone.utc)
            for i in sorted_items
        ]
        temps  = [float(i["temp_f"])   for i in sorted_items]
        humids = [float(i["humidity"]) for i in sorted_items]

        fig, ax1 = plt.subplots(figsize=(12, 5))
        ax2 = ax1.twinx()

        # Temperature (°F) — left axis, orange
        ax1.plot(datetimes, temps,  color="#FF6B35", linewidth=2.5,
                 label="Temp (°F)", zorder=2)
        ax1.fill_between(datetimes, temps, min(temps) - 1,
                         alpha=0.10, color="#FF6B35")

        # Humidity (%) — right axis, blue dashed
        ax2.plot(datetimes, humids, color="#4FC3F7", linewidth=2.0,
                 linestyle="--", label="Humidity (%)", zorder=2)

        # Labels + formatting
        last_ts = datetimes[-1].strftime("%Y-%m-%d %H:%M UTC")
        ax1.set_title(
            f"Syracuse, NY — Weather History\nLast updated: {last_ts}",
            fontsize=13, fontweight="bold", pad=10,
        )
        ax1.set_xlabel("Time (UTC)", labelpad=6)
        ax1.set_ylabel("Temperature (°F)", color="#FF6B35", labelpad=6)
        ax2.set_ylabel("Relative Humidity (%)", color="#4FC3F7", labelpad=6)
        ax1.tick_params(axis="y", labelcolor="#FF6B35")
        ax2.tick_params(axis="y", labelcolor="#4FC3F7")
        ax2.set_ylim(0, 110)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
        fig.autofmt_xdate(rotation=25, ha="right")

        # Combined legend
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2,
                   loc="upper left", fontsize=9, framealpha=0.85)
        if ax2.get_legend():
            ax2.get_legend().remove()

        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        buf.seek(0)
        plt.close(fig)
        log.info("Plot generated | size=%d bytes", len(buf.getvalue()))
        return buf

    except Exception as exc:
        log.exception("Plot generation failed: %s", exc)
        plt.close("all")
        return None


def _upload_plot(buf: io.BytesIO) -> str:
    """
    Upload a PNG buffer to S3 at a fixed key and return the public URL.
    The bucket must have a public-read bucket policy (see DEPLOY.md).
    """
    log.info("Uploading plot to s3://%s/%s", S3_BUCKET, PLOT_KEY)
    try:
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=PLOT_KEY,
            Body=buf.getvalue(),
            ContentType="image/png",
        )
        url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{PLOT_KEY}"
        log.info("Plot uploaded | url=%s", url)
        return url
    except Exception as exc:
        log.exception("S3 upload failed: %s", exc)
        raise


# ===========================================================================
# PART 2 — Chalice API routes
# ===========================================================================

@app.route("/")
def index():
    """
    Returns project description and list of callable resources.
    """
    log.info("GET /")
    return {
        "about": (
            "Tracks hourly weather in Syracuse, NY (temperature, humidity, and wind) "
            "using Open-Meteo API, DynamoDB, chalice, S3, lambda... plot regenerated hourly"
        ),
        "resources": ["current", "trend", "plot", "compare", "alerts"],
    }


@app.route("/current")
def current():
    """Return the most recent weather reading as a string."""
    log.info("GET /current")
    try:
        table = _get_table()
        resp  = table.query(
            KeyConditionExpression=Key("location_id").eq(CITY),
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])

        if not items:
            log.warning("GET /current — no data in DynamoDB yet")
            return {"response": "No data collected yet — the ingestion Lambda hasn't run. Check back soon!"}

        item = items[0]
        ts   = datetime.fromtimestamp(int(item["timestamp"]), tz=timezone.utc)
        msg  = (
            f"Syracuse, NY ({ts.strftime('%Y-%m-%d %H:%M UTC')}): "
            f"{float(item['temp_f']):.1f}°F | "
            f"Humidity {float(item['humidity']):.0f}% | "
            f"Wind {float(item['wind_mph']):.1f} mph"
        )
        log.info("GET /current → %s", msg)
        return {"response": msg}

    except Exception as exc:
        log.exception("GET /current failed: %s", exc)
        return {"response": f"Error fetching current weather: {exc}"}


@app.route("/trend")
def trend():
    """
    Return a trend summary: averages, min/max temp, and a warming/cooling
    direction computed by comparing the oldest vs newest 3 readings.
    """
    log.info("GET /trend")
    try:
        table = _get_table()
        items = _query_all(table)

        if not items:
            return {"response": "No data collected yet — check back after the first ingestion run."}

        temps  = [float(i["temp_f"])   for i in items]
        humids = [float(i["humidity"]) for i in items]
        n      = len(items)

        avg_t = sum(temps)  / n
        avg_h = sum(humids) / n
        min_t = min(temps)
        max_t = max(temps)

        # Trend direction: compare first-3 vs last-3 averages
        if n >= 6:
            early  = sum(temps[:3])  / 3
            recent = sum(temps[-3:]) / 3
            delta  = recent - early
            if   delta >  1.5:  direction = f"warming (+{delta:.1f}°F)"
            elif delta < -1.5:  direction = f"cooling ({delta:.1f}°F)"
            else:               direction = "stable"
            trend_str = f" Recent trend: {direction} over {n} readings."
        else:
            trend_str = f" ({n} reading(s) so far — more data needed for trend.)"

        msg = (
            f"Syracuse weather over {n} hourly readings: "
            f"avg {avg_t:.1f}°F (range {min_t:.1f}–{max_t:.1f}°F), "
            f"avg humidity {avg_h:.0f}%.{trend_str}"
        )
        log.info("GET /trend → %s", msg)
        return {"response": msg}

    except Exception as exc:
        log.exception("GET /trend failed: %s", exc)
        return {"response": f"Error computing trend: {exc}"}


@app.route("/plot")
def plot():
    """
    Return the public S3 URL of the latest weather chart PNG.
    The image is regenerated every hour by the ingest_weather Lambda.
    """
    log.info("GET /plot")
    url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{PLOT_KEY}"
    log.info("GET /plot → %s", url)
    return {"response": url}


@app.route("/compare")
def compare():
    """
    Compare the most recent weather reading to the reading from ~24 hours ago.
    Searches a ±30-minute window around the 24-hour-ago timestamp so a missed
    ingest run doesn't cause a total failure.
    Returns a graceful message if less than 24 hours of data has been collected.
    """
    log.info("GET /compare")
    try:
        table = _get_table()

        # --- Most recent reading -------------------------------------------
        resp_now = table.query(
            KeyConditionExpression=Key("location_id").eq(CITY),
            ScanIndexForward=False,
            Limit=1,
        )
        now_items = resp_now.get("Items", [])
        if not now_items:
            log.warning("GET /compare — no data in DynamoDB yet")
            return {"response": "No data collected yet — check back after the ingestion Lambda has run."}

        now = now_items[0]
        now_ts = int(now["timestamp"])

        # --- Reading from ~24 hours ago (±30 min window) ------------------
        target_ts  = now_ts - 86400          # exactly 24 hours back
        window_low  = str(target_ts - 1800)  # -30 minutes
        window_high = str(target_ts + 1800)  # +30 minutes

        log.info(
            "Searching for 24h-ago record | window=[%s, %s]",
            window_low, window_high,
        )

        from boto3.dynamodb.conditions import Key as _Key, Attr
        resp_ago = table.query(
            KeyConditionExpression=(
                _Key("location_id").eq(CITY) &
                _Key("timestamp").between(window_low, window_high)
            ),
            ScanIndexForward=False,
            Limit=1,
        )
        ago_items = resp_ago.get("Items", [])

        if not ago_items:
            # Work out how much data we actually have
            all_items = _query_all(table)
            if len(all_items) < 2:
                hours_collected = 0
            else:
                oldest_ts = int(all_items[0]["timestamp"])
                hours_collected = round((now_ts - oldest_ts) / 3600, 1)

            log.info(
                "GET /compare — no 24h-ago data found | hours_collected=%.1f",
                hours_collected,
            )
            return {
                "response": (
                    f"Not enough history yet — only ~{hours_collected}h of data collected "
                    f"(need 24h). Check back later!"
                )
            }

        ago = ago_items[0]
        ago_ts  = int(ago["timestamp"])
        ago_dt  = datetime.fromtimestamp(ago_ts,  tz=timezone.utc)
        now_dt  = datetime.fromtimestamp(now_ts,  tz=timezone.utc)

        now_temp  = float(now["temp_f"])
        ago_temp  = float(ago["temp_f"])
        now_humid = float(now["humidity"])
        ago_humid = float(ago["humidity"])
        now_wind  = float(now["wind_mph"])
        ago_wind  = float(ago["wind_mph"])

        delta_t = now_temp  - ago_temp
        delta_h = now_humid - ago_humid
        delta_w = now_wind  - ago_wind

        def _arrow(delta, threshold=0.5):
            if   delta >  threshold: return f"↑ +{delta:.1f}"
            elif delta < -threshold: return f"↓ {delta:.1f}"
            else:                    return f"→ {delta:+.1f}"

        msg = (
            f"Syracuse weather comparison — "
            f"Now ({now_dt.strftime('%m/%d %H:%M UTC')}) vs "
            f"24h ago ({ago_dt.strftime('%m/%d %H:%M UTC')}): "
            f"Temp {now_temp:.1f}°F {_arrow(delta_t)}°F | "
            f"Humidity {now_humid:.0f}% {_arrow(delta_h)}% | "
            f"Wind {now_wind:.1f} mph {_arrow(delta_w)} mph"
        )
        log.info("GET /compare → %s", msg)
        return {"response": msg}

    except Exception as exc:
        log.exception("GET /compare failed: %s", exc)
        return {"response": f"Error running comparison: {exc}"}


@app.route("/alerts")
def alerts():
    """
    Scan the full history and flag any readings where temperature or humidity
    deviated more than 2 standard deviations from the mean — likely weather
    events worth calling out. Returns up to the 5 most recent anomalies, or
    a 'no anomalies' message if everything looks normal.
    """
    log.info("GET /alerts")
    try:
        table = _get_table()
        items = _query_all(table)

        if len(items) < 6:
            log.info("GET /alerts — not enough data (%d records)", len(items))
            return {
                "response": (
                    f"Not enough history to detect anomalies yet "
                    f"({len(items)} record(s) — need at least 6). Check back soon!"
                )
            }

        temps  = [float(i["temp_f"])   for i in items]
        humids = [float(i["humidity"]) for i in items]

        # --- Compute mean and population std dev (pure Python, no numpy) --
        def _stats(values):
            n    = len(values)
            mean = sum(values) / n
            std  = math.sqrt(sum((x - mean) ** 2 for x in values) / n)
            return mean, std

        mean_t, std_t = _stats(temps)
        mean_h, std_h = _stats(humids)
        threshold = 2.0   # standard deviations

        log.info(
            "Anomaly thresholds | temp mean=%.2f std=%.2f | humid mean=%.2f std=%.2f",
            mean_t, std_t, mean_h, std_h,
        )

        # --- Flag anomalies -----------------------------------------------
        anomalies = []
        for item in items:
            ts    = int(item["timestamp"])
            dt    = datetime.fromtimestamp(ts, tz=timezone.utc)
            temp  = float(item["temp_f"])
            humid = float(item["humidity"])
            flags = []

            if abs(temp  - mean_t) > threshold * std_t:
                direction = "high" if temp > mean_t else "low"
                flags.append(f"temp {direction} ({temp:.1f}°F, avg {mean_t:.1f}°F)")

            if abs(humid - mean_h) > threshold * std_h:
                direction = "high" if humid > mean_h else "low"
                flags.append(f"humidity {direction} ({humid:.0f}%, avg {mean_h:.0f}%)")

            if flags:
                anomalies.append({
                    "ts": ts,
                    "dt": dt.strftime("%m/%d %H:%M UTC"),
                    "flags": flags,
                })

        log.info("GET /alerts — found %d anomaly record(s) out of %d", len(anomalies), len(items))

        if not anomalies:
            return {
                "response": (
                    f"No anomalies detected across {len(items)} readings "
                    f"(±2σ thresholds: temp {mean_t:.1f}±{threshold*std_t:.1f}°F, "
                    f"humidity {mean_h:.0f}±{threshold*std_h:.0f}%)."
                )
            }

        # Return the 5 most recent anomalies
        recent = sorted(anomalies, key=lambda x: x["ts"], reverse=True)[:5]
        lines  = [f"{a['dt']}: {', '.join(a['flags'])}" for a in recent]
        msg = (
            f"{len(anomalies)} anomaly/anomalies detected across {len(items)} readings "
            f"(±2σ). Most recent: {' | '.join(lines)}"
        )
        log.info("GET /alerts → %s", msg)
        return {"response": msg}

    except Exception as exc:
        log.exception("GET /alerts failed: %s", exc)
        return {"response": f"Error scanning for anomalies: {exc}"}
