#!/usr/bin/env bash
# Creates the security group for the copybot EC2 instance: SSH inbound from
# YOUR current IP only, no other inbound rule (dashboard.py is reached via
# an SSH tunnel, never a direct port — see infra/deploy/README.md).
#
# NOT executed automatically by anything — run manually, by hand, after
# reviewing the values below. Requires the AWS CLI already configured
# (`aws configure`) with credentials that can create security groups.
set -euo pipefail

# --- Fill these in before running --------------------------------------
VPC_ID="vpc-XXXXXXXX"     # `aws ec2 describe-vpcs --query 'Vpcs[].VpcId'`
REGION="us-east-1"
SG_NAME="copybot-sg"
# -------------------------------------------------------------------------

MY_IP="$(curl -fsS https://checkip.amazonaws.com)/32"
echo "Detected current public IP: $MY_IP"
echo "If you're on a VPN or your IP changes often, double-check this before continuing."

SG_ID=$(aws ec2 create-security-group \
  --group-name "$SG_NAME" \
  --description "Polymarket copybot: SSH-only inbound, no dashboard port exposed" \
  --vpc-id "$VPC_ID" \
  --region "$REGION" \
  --query 'GroupId' --output text)

echo "Created security group: $SG_ID"

# Inbound: SSH only, locked to this machine's current public IP — the only
# inbound rule this security group has.
aws ec2 authorize-security-group-ingress \
  --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$MY_IP" \
  --region "$REGION"

echo "Inbound rule added: TCP/22 from $MY_IP only."

# Outbound: AWS security groups already default to allow-all egress on
# creation (needed here: Polymarket's APIs + bullpen's backend are plain
# HTTPS to a changing set of hosts, so there's no fixed allow-list to scope
# this down to). This call is here for auditability, not because it's
# strictly required — it will report "already exists" on a fresh SG, which
# is expected, not an error.
aws ec2 authorize-security-group-egress \
  --group-id "$SG_ID" \
  --protocol -1 --cidr 0.0.0.0/0 \
  --region "$REGION" \
  2>&1 | grep -q "already exists" && echo "Outbound already unrestricted by default (expected)." || true

echo ""
echo "Done. Security group: $SG_ID"
echo "  Inbound:  TCP/22 (SSH) from $MY_IP only"
echo "  Outbound: unrestricted"
echo ""
echo "No inbound rule for port 8787 (dashboard.py) was created — intentional."
echo "Reach the dashboard via an SSH tunnel instead:"
echo "  ssh -L 8787:localhost:8787 <user>@<instance-ip>"
echo "  then open http://localhost:8787 in your local browser."
echo ""
echo "Next: use \$SG_ID ($SG_ID) when launching the EC2 instance."
