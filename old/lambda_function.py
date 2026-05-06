"""
lambda_function.py — Part 1 Ingestion Lambda
DS5220 Data Project 3

Triggered by an EventBridge scheduled rule (e.g. rate(15 minutes)).
Fetches current weather from Open-Meteo, writes a timestamped record to
DynamoDB, runs trend analysis vs. the previous reading, regenerates the
temperature+humidity plot, and uploads it to S3.

Required environment variables (set in the Lambda console):
  DYNAMODB_TABLE  — name of the DynamoDB table  (e.g. "weather-data")
  S3_BUCKET       — name of the S3 bucket for plots (e.g. "dp3-weather-plots")
  AWS_REGION      — optional, defaults to "us-east-1"
"""

import io
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import matplotlib
import matplotlib.pyplot as plt
import openmeteo_requests
import pandas as pd
import requests_cache
import seaborn as sns
from boto3.dynamodb.conditions import Key
from retry_requests import retry

matplotlib.use("Agg")  # non-interactive backend — required in Lambda

# ---------------------------------------------------------------------------
# Logging — CloudWatch picks this up automatically
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (from environment variables)
# ---------------------------------------------------------------------------
LATITUDE    = 43.23      # Syracuse, NY
LONGITUDE   = -76.14
LOCATION_ID = "SYRACUSE_NY"  # partition key in DynamoDB

TABLE_NAME = os.environ["DYNAMODB_TABLE"]
S3_BUCKET  = os.environ["S3_BUCKET"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Thresholds for weather-change alerts
TEMP_SPIKE_F    = Decimal("9.0")   # °F change in one interval → spike (≈ 5 °C)
HUMID_SPIKE_PCT = Decimal("20.0")  # % RH change in one interval → spike

PLOT_S3_KEY = f"weather/{LOCATION_ID.lower()}-weather.png"


# ---------------------------------------------------------------------------
# Step 1 — Fetch current weather from Open-Meteo
# ---------------------------------------------------------------------------
def fetch_weather() -> dict:
    """
    Call the Open-Meteo forecast API and return a DynamoDB-ready item dict.
    Uses a 15-minute request cache stored in /tmp (writable in Lambda) to
    avoid hammering the upstream API on retries.
    """
    log.info("Fetching weather from Open-Meteo | lat=%.4f lon=%.4f", LATITUDE, LONGITUDE)

    try:
        cache_session = requests_cache.CachedSession(
            "/tmp/.openmeteo_cache",  # /tmp is the only writable dir in Lambda
            expire_after=900,         # cache responses for 15 minutes
        )
        retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
        client = openmeteo_requests.Client(session=retry_session)

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude":         LATITUDE,
            "longitude":        LONGITUDE,
            "current":          ["temperature_2m", "relative_humidity_2m", "wind_speed_10m",
                                 "weather_code"],
            "temperature_unit": "fahrenheit",   # Open-Meteo returns °F natively
            "wind_speed_unit":  "mph",
            "timezone":         "auto",
        }

        responses = client.weather_api(url, params=params)
        r = responses[0]
        current = r.Current()

        temp_f     = current.Variables(0).Value()   # temperature_2m (°F)
        rh_pct     = current.Variables(1).Value()   # relative_humidity_2m
        wind_mph   = current.Variables(2).Value()   # wind_speed_10m (mph)
        wmo_code   = int(current.Variables(3).Value())  # WMO weather code

        log.info(
            "Open-Meteo response | lat=%.4f lon=%.4f | temp=%.2f°F | rh=%.1f%% "
            "| wind=%.1f mph | wmo_code=%d",
            r.Latitude(), r.Longitude(), temp_f, rh_pct, wind_mph, wmo_code,
        )

        return {
            "location_id":    LOCATION_ID,
            "timestamp":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "latitude":       Decimal(str(round(r.Latitude(),   6))),
            "longitude":      Decimal(str(round(r.Longitude(),  6))),
            "elevation_m":    Decimal(str(round(r.Elevation(),  1))),
            "temperature_f":  Decimal(str(round(temp_f,  2))),
            "humidity_pct":   Decimal(str(round(rh_pct,  1))),
            "wind_mph":       Decimal(str(round(wind_mph, 1))),
            "wmo_code":       wmo_code,
            "utc_offset_sec": int(r.UtcOffsetSeconds()),
        }

    except Exception as exc:
        log.exception("Failed to fetch weather from Open-Meteo: %s", exc)
        raise  # re-raise so Lambda marks the invocation as failed


# ---------------------------------------------------------------------------
# Step 2 — Query DynamoDB for the most recent previous entry
# ---------------------------------------------------------------------------
def get_previous(table) -> dict | None:
    """
    Return the most recent stored record for this location, or None if the
    table is empty. A missing previous record simply means it's the first run.
    """
    log.info("Querying DynamoDB for previous record | location_id=%s", LOCATION_ID)
    try:
        resp = table.query(
            KeyConditionExpression=Key("location_id").eq(LOCATION_ID),
            ScanIndexForward=False,   # newest first
            Limit=1,
        )
        items = resp.get("Items", [])
        if items:
            log.info("Previous record found | timestamp=%s", items[0].get("timestamp"))
            return items[0]
        log.info("No previous record found — this is the first ingestion run")
        return None
    except Exception as exc:
        log.exception("DynamoDB query for previous record failed: %s", exc)
        return None  # treat as first entry so we don't crash on startup


# ---------------------------------------------------------------------------
# Step 3 — Compute trend vs. previous reading
# ---------------------------------------------------------------------------
def weather_analysis(
    current: dict,
    previous: dict | None,
) -> tuple[str, Decimal, Decimal]:
    """
    Compare the current reading to the previous one and return
    (trend_label, delta_temp_f, delta_humidity_pct).

    Trend labels:
      FIRST_ENTRY      — no prior data
      STABLE           — both metrics within normal variance
      WARMING          — temperature rising noticeably
      COOLING          — temperature dropping noticeably
      TEMP_SPIKE       — abrupt temperature jump ≥ TEMP_SPIKE_F (front passage)
      HUMID_SPIKE      — abrupt humidity jump ≥ HUMID_SPIKE_PCT
      WARMING_DRYING   — getting warmer AND drier simultaneously
      COOLING_HUMID    — getting cooler AND more humid (precipitation likely)
    """
    if previous is None:
        log.info("Trend analysis | FIRST_ENTRY — no previous record")
        return "FIRST_ENTRY", Decimal("0"), Decimal("0")

    try:
        delta_t = current["temperature_f"] - Decimal(str(previous["temperature_f"]))
        delta_h = current["humidity_pct"]  - Decimal(str(previous["humidity_pct"]))

        if abs(delta_t) >= TEMP_SPIKE_F:
            trend = "TEMP_SPIKE"
        elif abs(delta_h) >= HUMID_SPIKE_PCT:
            trend = "HUMID_SPIKE"
        elif delta_t > Decimal("1.0") and delta_h < Decimal("-2"):
            trend = "WARMING_DRYING"
        elif delta_t < Decimal("-1.0") and delta_h > Decimal("2"):
            trend = "COOLING_HUMID"
        elif delta_t > Decimal("1.0"):
            trend = "WARMING"
        elif delta_t < Decimal("-1.0"):
            trend = "COOLING"
        else:
            trend = "STABLE"

        log.info(
            "Trend analysis | trend=%s | delta_temp=%+.2f°F | delta_humid=%+.1f%%",
            trend, delta_t, delta_h,
        )
        return trend, delta_t, delta_h

    except Exception as exc:
        log.exception("Trend analysis failed: %s", exc)
        return "STABLE", Decimal("0"), Decimal("0")


# ---------------------------------------------------------------------------
# Step 4 — Fetch full history from DynamoDB for plotting
# ---------------------------------------------------------------------------
def fetch_history(table) -> pd.DataFrame:
    """
    Return all stored records for this location as a DataFrame sorted by
    timestamp. Handles DynamoDB pagination so the full history is returned
    regardless of table size.
    """
    log.info("Fetching full history from DynamoDB | location_id=%s", LOCATION_ID)
    try:
        items = []
        kwargs = dict(
            KeyConditionExpression=Key("location_id").eq(LOCATION_ID),
            ScanIndexForward=True,
        )
        page = 0
        while True:
            resp = table.query(**kwargs)
            batch = resp.get("Items", [])
            items.extend(batch)
            page += 1
            log.info("Fetched page %d: %d records (total so far: %d)", page, len(batch), len(items))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

        if not items:
            log.warning("No history records found in DynamoDB")
            return pd.DataFrame()

        df = pd.DataFrame(items)
        df["timestamp"]     = pd.to_datetime(df["timestamp"])
        df["temperature_f"] = df["temperature_f"].astype(float)
        df["humidity_pct"]  = df["humidity_pct"].astype(float)
        df["delta_temp_f"]  = df["delta_temp_f"].astype(float)
        df["delta_humid"]   = df["delta_humid"].astype(float)
        log.info("History loaded: %d records spanning %s → %s",
                 len(df), df["timestamp"].min(), df["timestamp"].max())
        return df.sort_values("timestamp").reset_index(drop=True)

    except Exception as exc:
        log.exception("Failed to fetch history from DynamoDB: %s", exc)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Step 5 — Render dual-axis temperature + humidity plot
# ---------------------------------------------------------------------------
def generate_plot(df: pd.DataFrame) -> io.BytesIO | None:
    """
    Plot temperature (°F, left axis, orange) and relative humidity (%,
    right axis, blue) over time. Annotates notable weather events with
    icons and scatter markers.
    Returns a BytesIO PNG buffer, or None if there's not enough data yet.
    """
    if df.empty or len(df) < 2:
        log.info("Not enough history to plot yet (%d point(s)) — skipping", len(df))
        return None

    log.info("Generating plot for %d data points", len(df))
    try:
        sns.set_theme(style="darkgrid", context="talk", font_scale=0.9)
        fig, ax1 = plt.subplots(figsize=(14, 6))
        ax2 = ax1.twinx()

        # Temperature line (left axis)
        sns.lineplot(
            data=df, x="timestamp", y="temperature_f",
            ax=ax1, color="#FF6B35", linewidth=2.5, zorder=2, label="Temp (°F)",
        )
        ax1.fill_between(
            df["timestamp"], df["temperature_f"],
            df["temperature_f"].min() - 1,
            alpha=0.10, color="#FF6B35",
        )

        # Humidity line (right axis)
        sns.lineplot(
            data=df, x="timestamp", y="humidity_pct",
            ax=ax2, color="#4FC3F7", linewidth=2.0, linestyle="--",
            zorder=2, label="Humidity (%)",
        )

        # Annotate notable weather events
        event_map = {
            "TEMP_SPIKE":      ("⚡", "#FFD700", "Temp spike"),
            "HUMID_SPIKE":     ("💧", "#00BFFF", "Humid spike"),
            "COOLING_HUMID":   ("🌧", "#90EE90", "Cooling + humid"),
            "WARMING_DRYING":  ("☀️", "#FFA500", "Warming + drying"),
        }
        for event, (icon, color, label) in event_map.items():
            if "trend" not in df.columns:
                break
            subset = df[df["trend"] == event]
            if subset.empty:
                continue
            ax1.scatter(
                subset["timestamp"], subset["temperature_f"],
                color=color, s=120, zorder=4, label=f"{label} ({len(subset)})",
            )
            for _, row in subset.iterrows():
                ax1.annotate(
                    icon,
                    xy=(row["timestamp"], row["temperature_f"]),
                    xytext=(0, 14), textcoords="offset points",
                    ha="center", fontsize=15, zorder=5,
                )

        ax1.set_title(
            f"Weather — {LOCATION_ID.replace('_', ' ').title()}\n"
            f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            fontsize=14, fontweight="bold", pad=14,
        )
        ax1.set_xlabel("Time (UTC)", labelpad=8)
        ax1.set_ylabel("Temperature (°F)", color="#FF6B35", labelpad=8)
        ax2.set_ylabel("Relative Humidity (%)", color="#4FC3F7", labelpad=8)
        ax1.tick_params(axis="y", labelcolor="#FF6B35")
        ax2.tick_params(axis="y", labelcolor="#4FC3F7")
        ax2.set_ylim(0, 105)

        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(
            lines1 + lines2, labels1 + labels2,
            loc="upper left", fontsize=9, framealpha=0.85, edgecolor="#555555",
        )
        if ax2.get_legend():
            ax2.get_legend().remove()

        sns.despine(ax=ax1, top=True)
        sns.despine(ax=ax2, top=True, left=True)
        fig.autofmt_xdate(rotation=25, ha="right")
        plt.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        buf.seek(0)
        plt.close(fig)
        log.info("Plot generated | size=%d bytes | points=%d", len(buf.getvalue()), len(df))
        return buf

    except Exception as exc:
        log.exception("Plot generation failed: %s", exc)
        plt.close("all")
        return None


# ---------------------------------------------------------------------------
# Step 6 — Upload plot PNG to S3
# ---------------------------------------------------------------------------
def push_plot(buf: io.BytesIO) -> str:
    """
    Upload the PNG buffer to S3 and return the public URL.
    The object is uploaded without an ACL — access is controlled by the
    bucket policy set during setup (see README / AWS setup instructions).
    """
    log.info("Uploading plot to s3://%s/%s", S3_BUCKET, PLOT_S3_KEY)
    try:
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=PLOT_S3_KEY,
            Body=buf.getvalue(),
            ContentType="image/png",
        )
        url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{PLOT_S3_KEY}"
        log.info("Plot uploaded successfully | url=%s", url)
        return url
    except Exception as exc:
        log.exception("Failed to upload plot to S3: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Step 7 — Write record to DynamoDB
# ---------------------------------------------------------------------------
def write_to_dynamo(table, item: dict) -> None:
    """Write a single weather record to DynamoDB using PutItem."""
    log.info(
        "Writing record to DynamoDB | location_id=%s | timestamp=%s | trend=%s",
        item["location_id"], item["timestamp"], item.get("trend", "N/A"),
    )
    try:
        table.put_item(Item=item)
        log.info("DynamoDB write successful")
    except Exception as exc:
        log.exception("DynamoDB write failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------
def lambda_handler(event: dict, context) -> dict:
    """
    AWS Lambda handler. Orchestrates the full ingestion pipeline:
      1. Fetch current weather from Open-Meteo
      2. Get previous reading from DynamoDB (for trend analysis)
      3. Compute weather trend
      4. Write timestamped record to DynamoDB
      5. Fetch full history
      6. Generate plot
      7. Upload plot to S3

    Returns a JSON-serialisable summary dict. Unhandled exceptions bubble up
    and are recorded by Lambda as invocation failures in CloudWatch Logs.
    """
    log.info(
        "Lambda invoked | function=%s | request_id=%s",
        getattr(context, "function_name", "local"),
        getattr(context, "aws_request_id", "N/A"),
    )

    # --- AWS clients ---
    try:
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        table    = dynamodb.Table(TABLE_NAME)
        log.info("Connected to DynamoDB table: %s", TABLE_NAME)
    except Exception as exc:
        log.exception("Failed to connect to DynamoDB: %s", exc)
        raise

    # --- Fetch & analyse ---
    previous               = get_previous(table)
    entry                  = fetch_weather()
    trend, delta_t, delta_h = weather_analysis(entry, previous)

    entry["trend"]        = trend
    entry["delta_temp_f"] = delta_t
    entry["delta_humid"]  = delta_h

    # --- Persist ---
    write_to_dynamo(table, entry)

    # --- Human-readable log summary ---
    if trend == "FIRST_ENTRY":
        log.info(
            "SUMMARY | temp=%.2f°F | rh=%.1f%% | wind=%.1f mph | FIRST ENTRY",
            float(entry["temperature_f"]),
            float(entry["humidity_pct"]),
            float(entry.get("wind_mph", 0)),
        )
    else:
        alert = "  *** WEATHER EVENT ***" if trend in (
            "TEMP_SPIKE", "HUMID_SPIKE", "COOLING_HUMID", "WARMING_DRYING"
        ) else ""
        log.info(
            "SUMMARY | temp=%.2f°F (Δ%+.2f) | rh=%.1f%% (Δ%+.1f) "
            "| wind=%.1f mph | trend=%-16s%s",
            float(entry["temperature_f"]), float(delta_t),
            float(entry["humidity_pct"]),  float(delta_h),
            float(entry.get("wind_mph", 0)),
            trend, alert,
        )

    # --- Plot & upload ---
    plot_url = None
    history  = fetch_history(table)
    plot_buf = generate_plot(history)
    if plot_buf:
        plot_url = push_plot(plot_buf)

    result = {
        "statusCode": 200,
        "location_id": LOCATION_ID,
        "timestamp":   entry["timestamp"],
        "temperature_f": float(entry["temperature_f"]),
        "humidity_pct":  float(entry["humidity_pct"]),
        "wind_mph":      float(entry.get("wind_mph", 0)),
        "trend":         trend,
        "plot_url":      plot_url,
    }
    log.info("Lambda complete | result=%s", json.dumps(result))
    return result
