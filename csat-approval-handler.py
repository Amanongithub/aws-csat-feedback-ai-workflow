
import hashlib
import html
import json
import os
import time
import traceback

import boto3
from boto3.dynamodb.conditions import Key

s3 = boto3.client("s3")
ses = boto3.client("ses")
ddb = boto3.resource("dynamodb")

TABLE = ddb.Table(os.environ["APPROVAL_TABLE"])
BUCKET = os.environ["DATA_BUCKET"]
SENDER = os.environ["SES_SENDER"]
TEAM_EMAILS = [
    x.strip()
    for x in os.environ["TEAM_EMAILS"].split(",")
    if x.strip()
]
API_BASE_URL = os.environ["API_BASE_URL"].rstrip("/")


def find_report(token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = TABLE.query(
        IndexName="token_hash-index",
        KeyConditionExpression=Key("token_hash").eq(token_hash),
        Limit=1
    )

    items = result.get("Items", [])
    if not items:
        return None, token_hash

    item = items[0]
    if int(item["expires_at"]) <= int(time.time()):
        return None, token_hash

    if item["status"] != "PENDING_APPROVAL":
        return None, token_hash

    return item, token_hash


def html_response(content, status=200):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; connect-src https://oek5hdj34g.execute-api.ap-northeast-1.amazonaws.com; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        },
        "body": content
    }


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get(
        "http", {}
    ).get("method", "GET")
    path = event.get("rawPath", "")

    if method == "GET" and path.endswith("/review"):
        params = event.get("queryStringParameters") or {}
        token = params.get("token", "")

        if not token or len(token) > 200:
            return html_response("Invalid or missing review token.", 400)

        item, _ = find_report(token)
        if not item:
            return html_response(
                "This review link is invalid, expired, or already used.",
                410
            )

        safe_token = html.escape(token, quote=True)
        page = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CSAT report review</title>
</head>
<body style="font-family:Arial,sans-serif;max-width:640px;
margin:48px auto;padding:16px;line-height:1.6">
<h1>CSAT report review</h1>
<p>Report ID: {html.escape(item['report_id'])}</p>
<p>Review the report through your organization's approved process.
Choose one decision below.</p>
<button onclick="submitDecision('APPROVED')"
style="padding:12px;margin-right:8px">Approve and send to team</button>
<button onclick="submitDecision('REJECTED')"
style="padding:12px">Reject report</button>
<p id="result" role="status"></p>
<script>
async function submitDecision(decision) {{
  const result = document.getElementById('result');
  if (!confirm('Confirm decision: ' + decision + '?')) return;
  result.textContent = 'Submitting decision...';
  try {{
    const response = await fetch('{API_BASE_URL}/decision', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        token: '{safe_token}',
        decision: decision
      }})
    }});
    const data = await response.json();
    result.textContent = data.message || 'Request completed.';
  }} catch (e) {{
    result.textContent = 'Unable to submit. Contact the report owner.';
  }}
}}
</script>
</body>
</html>"""
        return html_response(page)

    if method == "POST" and path.endswith("/decision"):
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"message": "Invalid request body"})
            }

        token = body.get("token", "")
        decision = body.get("decision", "")

        if not token or len(token) > 200 or decision not in (
            "APPROVED", "REJECTED"
        ):
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"message": "Invalid decision request"})
            }

        item, token_hash = find_report(token)
        if not item:
            return {
                "statusCode": 410,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "message": "Link expired or decision already recorded."
                })
            }

        report_id = item["report_id"]
        now = int(time.time())

        try:
            TABLE.update_item(
                Key={"report_id": report_id},
                UpdateExpression=(
                    "SET #st = :decision, decision_at = :now"
                ),
                ConditionExpression=(
                    "#st = :pending AND token_hash = :hash "
                    "AND expires_at > :now"
                ),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":decision": decision,
                    ":pending": "PENDING_APPROVAL",
                    ":hash": token_hash,
                    ":now": now
                }
            )
        except Exception as exc:
            print(f"Decision update failed: {type(exc).__name__}")
            return {
                "statusCode": 409,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "message": "Decision could not be recorded. "
                               "The link may have expired or been used."
                })
            }

        if decision == "REJECTED":
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "message": "Report rejected. No team email was sent."
                })
            }

        # Only the approved JSON is sent to the team.
        try:
            response = s3.get_object(
                Bucket=BUCKET,
                Key=item["json_key"]
            )
            data = json.loads(
                response["Body"].read().decode("utf-8")
            )

            # Use a compact, readable representation of the analyzed data.
            report_text = json.dumps(
                data, indent=2, ensure_ascii=False
            )

            ses.send_email(
                Source=SENDER,
                Destination={"ToAddresses": TEAM_EMAILS},
                Message={
                    "Subject": {
                        "Data": "Manager-Approved CSAT Feedback Summary",
                        "Charset": "UTF-8"
                    },
                    "Body": {
                        "Text": {
                            "Data": (
                                "This CSAT report has been approved "
                                "by the manager for team review.\n\n"
                                + report_text
                            ),
                            "Charset": "UTF-8"
                        }
                    }
                }
            )

            TABLE.update_item(
                Key={"report_id": report_id},
                UpdateExpression="SET #st = :sent, sent_at = :now",
                ConditionExpression="#st = :approved",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":sent": "SENT",
                    ":approved": "APPROVED",
                    ":now": int(time.time())
                }
            )

            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "message": "Approved report sent to the team."
                })
            }

        except Exception as exc:
            print("Approved report delivery failed:")
            traceback.print_exc()
            # Status remains APPROVED if delivery fails. Investigate logs
            # and retry delivery through a controlled recovery procedure.
            return {
                "statusCode": 500,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "message": "Approval was recorded, but delivery "
                               "failed. Contact the report owner."
                })
            }

    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"message": "Route not found"})
    }
