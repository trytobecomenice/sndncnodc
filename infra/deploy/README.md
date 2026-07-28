# Copybot AWS deployment — how the pieces connect

Nothing here has been run. This is the audit trail for that decision — read
top to bottom, in order; each step's output feeds the next one.

## 1. `iam-policy-ec2-role.json` — attach to an IAM Role (not a user)

Scoped to exactly two actions: `ssm:GetParameter` on one specific parameter
ARN, and `kms:Decrypt` on the KMS key protecting it. Before creating this
policy, run `setup-secret.sh`'s Step 1 to resolve the real KMS key ARN
behind `alias/aws/ssm` — **an alias ARN does not work in an IAM policy's
`Resource` field for `kms:Decrypt`** (confirmed against AWS's own docs,
not assumed), so the placeholder `KMS_KEY_ARN_FROM_STEP_BELOW` must be
replaced with that resolved key ARN before this policy is created.

Create the role, attach this policy, then create an **instance profile**
wrapping the role and attach that instance profile to the EC2 instance at
launch time (or afterwards via `aws ec2 associate-iam-instance-profile`).
This is what lets `run.sh` call `aws ssm get-parameter` on the instance
with zero long-lived AWS access keys stored on the box.

## 2. `setup-secret.sh` — run once, from your own machine

Resolves the KMS key ARN (feeds back into step 1), then writes
`PRIVATE_POLYGON_RPC_URL` into SSM Parameter Store as a `SecureString`.
Replace the placeholder value with your real Alchemy/QuickNode URL before
running. Never committed, never logged anywhere persistent.

## 3. `create-security-group.sh` — run once, from your own machine

Creates the security group: inbound TCP/22 from your current IP only,
outbound unrestricted (Polymarket's APIs + bullpen's backend are plain
HTTPS to a changing set of hosts, so there's nothing narrower to scope
egress to). No inbound rule for port 8787 — the dashboard is reached only
via an SSH tunnel (`ssh -L 8787:localhost:8787 <user>@<instance-ip>`),
never a direct port. Note this hardcodes your IP at the time you run it —
re-run (or manually update the rule) if your IP changes.

## 4. Launch the EC2 instance

Using the security group from step 3 and the instance profile from step 1.
Not scripted here since instance sizing/AMI/key-pair choice are worth
picking deliberately together, not defaulted in a script.

## 5. Bullpen auth migration (one-time, manual, before first deploy)

This is the part that touches live credentials, so it's deliberately not
automated:
1. Locally: `bullpen doctor deploy-auth` — confirms the credential bundle
   is actually portable before anything is copied anywhere.
2. `scp -r ~/.bullpen <user>@<instance-ip>:/opt/copybot/bullpen-home` — a
   one-time, manual, direct machine-to-machine copy over SSH. Never
   through S3, never through a general-purpose file share.
3. On the instance: `BULLPEN_HOME=/opt/copybot/bullpen-home bullpen login --non-interactive`
   to confirm the copied session is recognized before the container ever
   starts.

## 6. Database: copy the existing schema, don't start from empty

**Flagging this explicitly since it's easy to miss**: `db.py` never runs
`CREATE TABLE` — schema/migrations are owned by `packages/db` (see
`docs/copy-trading/RISK_MANAGEMENT.md` Rule 8). An empty `/opt/copybot/data`
directory on a fresh instance means the container will fail on its first
query, not start cleanly. Copy the real local `data/app.db` (with its
schema and, if you want continuity, its history) to
`/opt/copybot/data/app.db` on the instance before the first `run.sh` — or
run the TS migration tooling (`pnpm db:migrate`) against a fresh file there
first if you'd rather start the cloud instance's history clean.

## 7. Build and ship the image

```
docker build -t copybot:latest -f infra/deploy/Dockerfile .
docker save copybot:latest | gzip | ssh <user>@<instance-ip> 'gunzip | docker load'
```
(Or push to a private registry — ECR — if you'd rather not `scp` image
tarballs; not scripted here since it's a one-line decision either way.)

## 8. `run.sh` — run on the EC2 host itself

The connecting piece: reads the SSM parameter (via the instance's IAM role
from step 1 — no stored AWS keys), then `docker run`s the image with that
value injected as a plain env var and the two persistent directories
(bullpen credentials, SQLite data) bind-mounted in. Set up `run.sh` itself
to run via a systemd unit (`ExecStart=/opt/copybot/run.sh`) so the
container comes back after an instance reboot too, not just a container
crash (`--restart unless-stopped` alone only covers the latter).

---

**Everything above is a draft for your review — nothing has been executed,
no AWS resources created, no credentials copied anywhere.** Let me know
what you'd like changed before we run any of it for real.
