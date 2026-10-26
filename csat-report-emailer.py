
import hashlib
import html
import json
import os
import secrets
import time
import uuid

import boto3
from urllib.parse import quote

s3 = boto3.client("s3")
ses = boto3.client("ses")
ddb = boto3.resource("dynamodb")

BUCKET = os.environ["DATA_BUCKET"]
TABLE = ddb.Table(os.environ["APPROVAL_TABLE"])
SENDER = os.environ["SES_SENDER"]
MANAGER = os.environ["MANAGER_EMAIL"]
API_BASE_URL = os.environ["API_BASE_URL"].rstrip("/")
TOKEN_TTL_SECONDS = 48 * 60 * 60


def humanize(key):
    return str(key).replace("_", " ").replace("-", " ").capitalize()


def to_bullets(data, prefix=""):
    lines = []

    if isinstance(data, dict):
        for key, value in data.items():
            label = f"{prefix}{humanize(key)}"
            if isinstance(value, (dict, list)):
                lines.extend(to_bullets(value, label + " — "))
            else:
                lines.append(f"- {label}: {value}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                lines.extend(to_bullets(item, prefix))
            else:
                lines.append(f"- {prefix}{item}")
    else:
        lines.append(f"- {prefix}{data}")

    return lines


def lambda_handler(event, context):
    for record in event.get("Records", []):
        if record.get("eventSource") != "aws:s3":
            continue

        key = record["s3"]["object"]["key"]
        from urllib.parse import unquote_plus
        key = unquote_plus(key)

        if not key.startswith("processed/") or not key.endswith(".json"):
            continue

        response = s3.get_object(Bucket=BUCKET, Key=key)
        data = json.loads(response["Body"].read().decode("utf-8"))

        report_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time())
        expires_at = now + TOKEN_TTL_SECONDS

        TABLE.put_item(
            Item={
                "report_id": report_id,
                "token_hash": token_hash,
                "status": "PENDING_APPROVAL",
                "json_key": key,
                "created_at": now,
                "expires_at": expires_at,
                "manager_email": MANAGER
            },
            ConditionExpression="attribute_not_exists(report_id)"
        )

        bullets = to_bullets(data)
        findings = "\n".join(bullets) or "- No findings were present."

        review_url = (
            f"{API_BASE_URL}/review?token={quote(token)}"
        )

        text_body = f"""Hello,

A CSAT feedback report is ready for your review.

Report ID: {report_id}

Key findings from the generated analysis:
{findings}

Please review the report and approve or reject it here:
{review_url}

This approval link expires in 48 hours. The team will not receive
the report unless you approve it.

Regards,
CSAT Feedback Analyzer
"""

        ses.send_email(
            Source=SENDER,
            Destination={"ToAddresses": [MANAGER]},
            Message={
                "Subject": {
                    "Data": "CSAT Feedback Report — Approval Required",
                    "Charset": "UTF-8"
                },
                "Body": {
                    "Text": {
                        "Data": text_body,
                        "Charset": "UTF-8"
                    }
                }
            }
        )

    return {"statusCode": 200, "body": "Report email processed"}
