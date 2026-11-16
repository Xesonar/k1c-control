#!/usr/bin/env python3
"""Explicit TLS activation phases; never restarts the bot, media worker, or printer."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess


HERE = Path(__file__).resolve().parent
CERTBOT = '/opt/k1c-certbot/bin/certbot'
CHALLENGE_ROOT = Path('/var/www/k1c-acme')
SITE = Path('/etc/nginx/sites-available/k1c-web')
ENABLED = Path('/etc/nginx/sites-enabled/k1c-web')


def run(args, timeout=40):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {(result.stdout + result.stderr)[-3000:]}')
    return result.stdout.strip()


def render(source, destination, ip):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text((HERE / source).read_text().replace('__PUBLIC_IP__', ip))
    os.chmod(destination, 0o644)


def backup_nginx(base):
    target = base / 'nginx-before-activation'
    if not target.exists():
        shutil.copytree('/etc/nginx', target, symlinks=True)


def install_site(ip, https):
    render('nginx-bootstrap.conf.in' if not https else 'nginx-https.conf.in', SITE, ip)
    if https:
        shutil.copy2(HERE / 'nginx-http-settings.conf', '/etc/nginx/conf.d/k1c-http-settings.conf')
        render('nginx-proxy.conf.in', Path('/etc/nginx/snippets/k1c-proxy.conf'), ip)
    default = Path('/etc/nginx/sites-enabled/default')
    if default.is_symlink():
        default.unlink()
    if ENABLED.is_symlink():
        if ENABLED.resolve() != SITE:
            raise RuntimeError('unexpected k1c-web enabled-site target')
    elif ENABLED.exists():
        raise RuntimeError('k1c-web enabled-site is not a symlink')
    else:
        ENABLED.symlink_to(SITE)
    run(['/usr/sbin/nginx', '-t'])


def start_or_reload():
    run(['systemctl', 'unmask', 'nginx.service'])
    run(['systemctl', 'enable', 'nginx.service'])
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'nginx.service']).returncode == 0:
        run(['systemctl', 'reload', 'nginx.service'])
    else:
        run(['systemctl', 'start', 'nginx.service'])


def install_renewal(ip):
    for name in ('k1c-certbot-renew.service', 'k1c-certbot-renew.timer'):
        text = (HERE / name).read_text().replace('__PUBLIC_IP__', ip)
        destination = Path('/etc/systemd/system') / name
        destination.write_text(text)
        os.chmod(destination, 0o644)
    shutil.copy2(HERE / 'k1c-cert-reload', '/usr/local/sbin/k1c-cert-reload')
    os.chmod('/usr/local/sbin/k1c-cert-reload', 0o755)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'k1c-certbot-renew.timer'])


def main(args):
    if os.geteuid() != 0:
        raise RuntimeError('run as root on the authorized VPS')
    ip = str(ipaddress.IPv4Address(args.ip))
    base = args.release_dir.resolve()
    if not base.is_dir() or not str(base).startswith('/root/k1c-web-release-'):
        raise RuntimeError('existing release/backup directory required')
    if args.phase == 'challenge':
        # This is the first phase that opens any new public listener.
        backup_nginx(base)
        CHALLENGE_ROOT.mkdir(parents=True, exist_ok=True)
        (CHALLENGE_ROOT / '.well-known/acme-challenge').mkdir(parents=True, exist_ok=True)
        install_site(ip, https=False)
        start_or_reload()
    elif args.phase in ('issue-staging', 'issue'):
        if not args.agree_tos:
            raise RuntimeError('pass --agree-tos only after authorization for ACME issuance')
        version = run([CERTBOT, '--version'])
        pieces = version.split()[-1].split('.')
        if tuple(int(v) for v in pieces[:2]) < (5, 4):
            raise RuntimeError('Certbot >=5.4 required for IP webroot support')
        command = [CERTBOT, 'certonly', '--non-interactive', '--agree-tos',
                   '--register-unsafely-without-email', '--webroot',
                   '--webroot-path', str(CHALLENGE_ROOT), '--ip-address', ip,
                   '--cert-name', ip, '--preferred-profile', 'shortlived']
        if args.phase == 'issue-staging':
            command += ['--staging', '--config-dir', '/etc/letsencrypt-staging',
                        '--work-dir', '/var/lib/letsencrypt-staging',
                        '--logs-dir', '/var/log/letsencrypt-staging']
        run(command, timeout=180)
    elif args.phase == 'https':
        certificate = Path('/etc/letsencrypt/live') / ip
        run(['/usr/bin/openssl', 'x509', '-in', str(certificate / 'cert.pem'),
             '-noout', '-checkend', '86400'])
        san = run(['/usr/bin/openssl', 'x509', '-in', str(certificate / 'cert.pem'),
                   '-noout', '-ext', 'subjectAltName'])
        if f'IP Address:{ip}' not in san:
            raise RuntimeError('certificate does not cover the public IP')
        # A future source deployment must have started and tested the authenticated app.
        with socket.create_connection(('127.0.0.1', 3340), timeout=3):
            pass
        backup_nginx(base)
        install_site(ip, https=True)
        start_or_reload()
        install_renewal(ip)
    print(json.dumps({'phase': args.phase, 'ip': ip, 'completed': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('challenge', 'issue-staging', 'issue', 'https'))
    parser.add_argument('--release-dir', type=Path, required=True)
    parser.add_argument('--ip', required=True)
    parser.add_argument('--agree-tos', action='store_true')
    main(parser.parse_args())
