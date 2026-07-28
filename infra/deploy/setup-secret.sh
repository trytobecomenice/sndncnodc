#!/usr/bin/env bash
# One-time setup: (1) resolve the KMS key ARN behind the AWS-managed
# alias/aws/ssm key (needed for iam-policy-ec2-role.json's Resource field —
# an alias ARN does NOT work there, only the real key ARN does), then
# (2) write PRIVATE_POLYGON_RPC_URL into SSM Parameter Store as a
# SecureString.
#
# Run this BY HAND, from your own machine, never from a script that logs
# its own output anywhere persistent. Not executed automatically.
set -euo pipefail

REGION="us-east-1"
PARAM_NAME="/copybot/prod/polygon_rpc_url"

# --- Step 1: resolve the real KMS key ARN behind alias/aws/ssm -------------
# (IAM policies cannot reference a KMS alias ARN directly for kms:Decrypt —
# confirmed against AWS's own docs; only the underlying key ARN works.)
KMS_KEY_ARN=$(aws kms describe-key \
  --key-id alias/aws/ssm \
  --region "$REGION" \
  --query 'KeyMetadata.Arn' --output text)

echo "Resolved KMS key ARN: $KMS_KEY_ARN"
echo "Paste this into infra/deploy/iam-policy-ec2-role.json's"
echo "\"DecryptViaAwsManagedSsmKey\" statement's Resource field, replacing"
echo "KMS_KEY_ARN_FROM_STEP_BELOW, before creating the IAM policy."
echo ""

# --- Step 2: write the actual secret ---------------------------------------
# Tip: prefix this line with a space if your shell has HISTCONTROL=ignorespace
# set, so the real value doesn't land in ~/.bash_history / ~/.zsh_history.
# Replace the --value below with your real Alchemy/QuickNode URL before running.
aws ssm put-parameter \
  --name "$PARAM_NAME" \
  --type "SecureString" \
  --value "https://polygon-mainnet.g.alchemy.com/v2/REPLACE_WITH_YOUR_REAL_KEY" \
  --region "$REGION" \
  --overwrite

echo "Stored $PARAM_NAME as a SecureString (encrypted with alias/aws/ssm)."
echo "It is never written to config.py or the Docker image — see"
echo "infra/deploy/run.sh for how it's read at container-start time."
