#!/usr/bin/env bash
# Runs on the EC2 HOST (not inside the container) — this is the connecting
# piece between the SSM parameter, the IAM role, and the Docker image. It
# reads the secret using the instance's attached IAM role (no AWS access
# keys stored on the box at all) and hands the plain value to `docker run`
# as a single env var. The AWS CLI must be installed on the host (Amazon
# Linux 2023 ships it by default; other AMIs may need it installed).
set -euo pipefail

REGION="us-east-1"
PARAM_NAME="/copybot/prod/polygon_rpc_url"
IMAGE="copybot:latest"
BULLPEN_HOME_HOST_DIR="/opt/copybot/bullpen-home"   # the copied, authenticated ~/.bullpen — see README
DATA_HOST_DIR="/opt/copybot/data"                    # SQLite lives here

# Relies on the instance's IAM role (attached via instance profile) for
# credentials — nothing else needed. Fails loudly if the role can't read
# the parameter, rather than silently starting the bot with no RPC override.
RPC_URL=$(aws ssm get-parameter \
  --name "$PARAM_NAME" \
  --with-decryption \
  --region "$REGION" \
  --query 'Parameter.Value' --output text)

# uid 10001 matches the Dockerfile's `copybot` user — the bind-mounted
# BULLPEN_HOME must be writable by that user (bullpen updates session/log
# state there at runtime).
chown -R 10001:10001 "$BULLPEN_HOME_HOST_DIR" "$DATA_HOST_DIR"

docker run \
  --name copybot \
  --restart unless-stopped \
  --detach \
  -e PRIVATE_POLYGON_RPC_URL="$RPC_URL" \
  -v "$BULLPEN_HOME_HOST_DIR:/home/copybot/.bullpen" \
  -v "$DATA_HOST_DIR:/app/data" \
  "$IMAGE"

echo "copybot container started. Logs: docker logs -f copybot"
