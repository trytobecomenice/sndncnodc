# Lightweight runtime image for the Polymarket copy-trading bot (paper mode
# only — see docs/copy-trading/SAFETY.md §6). Zero third-party Python
# packages (matches requirements.txt) — the only non-stdlib runtime
# dependency is the bullpen CLI itself, installed below, not pip-installed.
#
# Secrets and persistent state are DELIBERATELY NOT part of this image:
#   - PRIVATE_POLYGON_RPC_URL is injected as an env var at `docker run` time
#     by infra/deploy/run.sh (sourced from SSM Parameter Store on the host,
#     via the EC2 instance's IAM role — never baked into a layer here).
#   - BULLPEN_HOME (the authenticated credential store) is a bind-mounted
#     volume, not copied into the image — see run.sh and
#     infra/deploy/README.md for the one-time migration procedure.
FROM python:3.13-slim

# ca-certificates: TLS verification for Polymarket's public APIs + bullpen's
# own backend. curl: fetches the bullpen CLI installer. Installed and the
# apt cache dropped in the same layer to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Install the bullpen CLI (not a pip package — see requirements.txt). Runs
# as root during the build; the binary is moved to a stable global path and
# the rest of the installer's root-owned scratch dir (config.toml, an empty
# credentials dir, etc.) is discarded — the REAL, authenticated credential
# store is mounted fresh at container-start time (see run.sh), never
# baked into this image.
RUN curl -fsSL https://cli.bullpen.fi/install.sh | bash \
    && mv /root/.bullpen/bin/bullpen /usr/local/bin/bullpen \
    && rm -rf /root/.bullpen

WORKDIR /app

# App source only. No `pip install -r requirements.txt` step: there are no
# third-party Python packages to install (bot.py/db.py/etc. use stdlib
# only — see requirements.txt's own comment on this).
COPY bot.py config.py db.py bullpen_client.py polymarket_data_api.py \
     polymarket_simulator.py risk_manager.py ./

# Non-root runtime user. The BULLPEN_HOME volume mounted at `docker run`
# time must be chown'd to this same uid on the host first — see run.sh.
RUN useradd --create-home --uid 10001 copybot \
    && chown -R copybot:copybot /app
USER copybot

# Actual value is a bind-mounted volume (see run.sh's -v flag), not baked
# in here — this only tells bullpen/bot.py where to look for it.
ENV BULLPEN_HOME=/home/copybot/.bullpen

# SIGTERM is Docker's default stop signal and is already handled gracefully
# by bot.py's own signal handler (state save + clean shutdown, confirmed in
# bot.out.log: "SIGTERM received — finishing current work...") — no custom
# STOPSIGNAL needed.
ENTRYPOINT ["python3", "-u", "bot.py"]
