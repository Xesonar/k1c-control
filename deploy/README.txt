K1C deployment infrastructure

Production application remains /opt/k1c-telegram. The HTTP server is embedded in
the bot process and binds only to127.0.0.1:3340. Nginx proxies HTTPS to it; every
API/static/media authorization decision remains in the application. No nginx media alias.

Paths:
  /opt/k1c-web/static and /opt/k1c-web/service: prepared deployment assets
  /opt/k1c-web/releases: release metadata/assets
  /opt/k1c-certbot: isolated Python environment for Certbot>=5.4
  /var/www/k1c-acme: HTTP-01 challenge files only
  /var/lib/k1c-media: media.sqlite3/archive/rolling/tmp/timelapse,0700
  /etc/k1c-media/config.json: separate worker configuration,0600

The verified K1C H264 source is configured with media_source_url, currently
http://printer.local:1984/api/stream.mp4?src=chassis. The worker also accepts
the legacy media_rtsp_url key; RTSP transport options apply only to RTSP URLs.
Recording copies the existing H264 stream on the VPS without re-encoding it.
Do not change or restart the printer's camera services during deployment.

Preparation: python3 prepare_infrastructure.py --release-dir /root/k1c-web-release-TIMESTAMP
Installs nginx/FFmpeg/ffprobe/python3-venv, creates the isolated Certbot environment,
and leaves nginx MASKED/inactive. Package startup is blocked with a temporary
policy-rc.d, restored byte-for-byte afterward. NEEDRESTART_MODE=l prevents the
package transaction from automatically restarting running services. Production
bot/slicer/VPN PIDs and states are compared before/after. No certificate or listener.

TLS activation is deliberately separate and requires the coordinating agent's
explicit go-ahead after staged source tests and application readiness:
  python3 activate_tls.py challenge --ip YOUR_PUBLIC_IP --release-dir /root/k1c-web-release-TIMESTAMP
  python3 activate_tls.py issue-staging --ip YOUR_PUBLIC_IP --agree-tos --release-dir /root/k1c-web-release-TIMESTAMP
  python3 activate_tls.py issue --ip YOUR_PUBLIC_IP --agree-tos --release-dir /root/k1c-web-release-TIMESTAMP
  python3 activate_tls.py https --ip YOUR_PUBLIC_IP --release-dir /root/k1c-web-release-TIMESTAMP

The challenge phase opens port80 with ACME/404 only. Staging uses separate ACME
directories and cannot replace a trusted live certificate. The https phase checks
IP SAN/remaining lifetime and loopback app availability before installing443 proxy
and the six-hour renewal timer. It restarts/reloads nginx only; it never restarts
k1c-telegram, k1c-slicer, VPN, go2rtc, Klipper, or the printer.

Nginx:130MiB request-body limit; backend enforces20MiBSTL/128MiBGcode. Login
POST/api/login has per-IP10/min rate plus burst5; backend also limits username/IP.
Forwarding headers are overwritten; no client-supplied forwarding chain is trusted.
Access logs omit query strings, cookies, Authorization, request bodies and referers.

Media unit is a template pending worker CLI verification and explicit capture
authorization. Installing it must not automatically start it. Both services use
the same static no-login system account,k1c-runtime, with0700 state directories.
The current production bot uses DynamicUser and a/var/lib/private symlink; apply
k1c-telegram-web.conf only with the reviewed state-directory migration, fresh
consistentSQLite backup, pre/postACL/workflow hashes and rollback support.
A mount namespace hides botSQLite,/etc/k1c-telegram and/run/credentials from
the media worker; capabilities are empty, and only/var/lib/k1c-media is writable.
It does not expose files publicly or allow playback as a deployment check.

Runtime cutover (explicit service stop/start is the caller's responsibility):
  python3 runtime_cutover.py prepare --release-dir /root/k1c-web-release-TIMESTAMP
  systemctl stop k1c-telegram
  python3 runtime_cutover.py install --release-dir RELEASE --backup PREPARED_BACKUP
Install takes an additional consistent SQLite backup after the bot has stopped,
moves only the known DynamicUser state link, and compares protected state hashes.
It preserves every existing bot configuration field and adds the web/media keys.
It installs media_capture_enabled=false, so starting the worker alone cannot
begin recording. Enable recording only at the authorized final activation step.
Bootstrap the owner through web_auth.py JSON stdin; do not place credentials in
arguments, source, logs, release receipts, or backup metadata.
Rollback restores the previous source/config/unit and state directory layout:
  python3 runtime_cutover.py rollback --release-dir RELEASE --backup PREPARED_BACKUP
It preserves current SQLite data and does not restore the old snapshot.

Migration offline checks:
  python3 -m unittest discover -s deploy -p test_runtime_cutover.py -v

Checks before publishing:
- Verify full source manifest and run offline stage tests with isolatedDB/config.
- Take fresh consistentSQLite.backup and save ACL/workflow/queue hashes before stopping bot.
- Restart only explicitly authorized application units; compare hashes/PIDs afterward.
- Verify login/CSRF/RBAC and metadata endpoints without requesting camera/video content.
- No test Telegram messages or credentials in argv, logs, repository, or chat.

Primary references:
https://letsencrypt.org/2026/03/11/shorter-certs-certbot
https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability
https://certbot.eff.org/faq/

Runtime cutover install also requires --web-origin https://dashboard.example.com.
Configure owner_id in the private bot config before installation.
