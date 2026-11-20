#!/bin/zsh
# Deploy monitor.py and its timers under /opt/catalyst-monitor.
# The monitor's SSH key is created there once; its public key goes into the data server's authorized_keys with a
# forced command (monitor-gate), the monitor host's address only, and no pty or forwarding.
#   CATALYST_MONITOR_HOST=USER@HOST CATALYST_DEPLOY_HOST=USER@HOST deploy/deploy_monitor.sh
set -e
O=(-o ControlMaster=no -o ControlPath=none -o ConnectTimeout=20 -o BatchMode=yes -o ClearAllForwardings=yes)
MON=${CATALYST_MONITOR_HOST:-}
US=${CATALYST_DEPLOY_HOST:-}
[[ -n "$MON" ]] || { echo "CATALYST_MONITOR_HOST is required (USER@HOST)" >&2; exit 2; }
[[ -n "$US" ]] || { echo "CATALYST_DEPLOY_HOST is required (USER@HOST)" >&2; exit 2; }
MON_IP=${MON#*@}
cd ${0:A:h}
scp -q $O monitor/monitor.py monitor/catalyst-monitor-*.service monitor/catalyst-monitor-*.timer $MON:/tmp/
ssh $O $MON "set -e; mkdir -p /opt/catalyst-monitor/backup; chmod 700 /opt/catalyst-monitor
  install -m 755 /tmp/monitor.py /opt/catalyst-monitor/monitor.py
  mv /tmp/catalyst-monitor-*.service /tmp/catalyst-monitor-*.timer /etc/systemd/system/; rm -f /tmp/monitor.py
  test -f /opt/catalyst-monitor/id_monitor || ssh-keygen -q -t ed25519 -N '' -C catalyst-monitor -f /opt/catalyst-monitor/id_monitor
  grep -q '^CATALYST_US_HOST=' /opt/catalyst-monitor/.env || echo 'CATALYST_US_HOST=$US' >> /opt/catalyst-monitor/.env
  chmod 600 /opt/catalyst-monitor/.env
  systemctl daemon-reload"
PUB=$(ssh $O $MON cat /opt/catalyst-monitor/id_monitor.pub)
LINE="from=\"$MON_IP\",command=\"/opt/catalyst/bin/monitor-gate\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty $PUB"
ssh $O $US "grep -qF '$PUB' ~/.ssh/authorized_keys || echo '$LINE' >> ~/.ssh/authorized_keys; grep -c catalyst-monitor ~/.ssh/authorized_keys"
ssh $O $MON "systemctl enable -q --now catalyst-monitor-check.timer catalyst-monitor-daily.timer catalyst-monitor-pull-backup.timer catalyst-monitor-restore-drill.timer; systemctl list-timers 'catalyst-monitor-*' --no-pager"
