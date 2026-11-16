#!/usr/bin/env python3
"""Install prerequisites without starting nginx or restarting production units."""
import argparse
import json
import os
from pathlib import Path
import subprocess


UNITS = ('k1c-telegram', 'k1c-slicer')
CERTBOT_ENV = Path('/opt/k1c-certbot')
CERTBOT_REQUIREMENT = 'certbot==5.10.0'


def run(args, *, timeout=30, env=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)


def unit_state(name):
    result = run(['systemctl', 'show', name, '--property=MainPID,ActiveState,SubState'])
    if result.returncode:
        raise RuntimeError(f'cannot inspect {name}')
    return result.stdout.strip()


def listening_web_ports():
    result = run(['ss', '-lnt'])
    if result.returncode:
        raise RuntimeError('cannot inspect listeners')
    return [line for line in result.stdout.splitlines()[1:]
            if line.split()[3].rsplit(':', 1)[-1] in ('80', '443')]


def prepare(base):
    if os.geteuid() != 0:
        raise RuntimeError('run as root on the authorized VPS')
    base = base.resolve()
    if not base.is_dir() or not str(base).startswith('/root/k1c-web-release-'):
        raise RuntimeError('existing release/backup directory required')
    if listening_web_ports():
        raise RuntimeError('port 80/443 already occupied; do not change an existing web server')
    before = {unit: unit_state(unit) for unit in UNITS}
    policy = Path('/usr/sbin/policy-rc.d')
    existed = policy.exists() or policy.is_symlink()
    old_link = os.readlink(policy) if policy.is_symlink() else None
    old_data = policy.read_bytes() if existed and old_link is None else None
    old_mode = policy.stat().st_mode & 0o777 if existed and old_link is None else None
    if old_data is not None:
        (base / 'policy-rc.d.original').write_bytes(old_data)
        os.chmod(base / 'policy-rc.d.original', 0o600)
    result = {'before': before, 'steps': []}
    try:
        if policy.is_symlink():
            policy.unlink()
        policy.write_text('#!/bin/sh\nexit 101\n')
        os.chmod(policy, 0o755)
        masked = run(['systemctl', 'mask', 'nginx.service'])
        if masked.returncode:
            raise RuntimeError('cannot mask nginx before package installation')
        env = dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l')
        steps = [
            ('apt-update', ['apt-get', 'update', '-qq'], 150),
            ('apt-install', ['apt-get', 'install', '-y', '--no-install-recommends',
                             '--no-upgrade', 'nginx', 'ffmpeg', 'python3-venv'], 220),
        ]
        for name, args, timeout in steps:
            process = run(args, timeout=timeout, env=env)
            (base / f'{name}.log').write_text(process.stdout + process.stderr)
            result['steps'].append({'step': name, 'exit': process.returncode})
            if process.returncode:
                raise RuntimeError(f'{name} failed; see server-side log')
        if not (CERTBOT_ENV / 'bin/python').exists():
            process = run(['/usr/bin/python3', '-m', 'venv', str(CERTBOT_ENV)], timeout=45)
            if process.returncode:
                raise RuntimeError('certbot venv creation failed')
        process = run([str(CERTBOT_ENV / 'bin/python'), '-m', 'pip', 'install',
                       CERTBOT_REQUIREMENT], timeout=150)
        (base / 'certbot-install.log').write_text(process.stdout + process.stderr)
        result['steps'].append({'step': 'certbot-venv-install', 'exit': process.returncode})
        if process.returncode:
            raise RuntimeError('certbot installation failed; see server-side log')
        freeze = run([str(CERTBOT_ENV / 'bin/python'), '-m', 'pip', 'freeze'])
        (base / 'certbot-freeze.txt').write_text(freeze.stdout)
        result['versions'] = {}
        for name, args in {
            'python': ['/usr/bin/python3', '--version'],
            'ffmpeg': ['/usr/bin/ffmpeg', '-version'],
            'ffprobe': ['/usr/bin/ffprobe', '-version'],
            'nginx': ['/usr/sbin/nginx', '-v'],
            'certbot': [str(CERTBOT_ENV / 'bin/certbot'), '--version'],
        }.items():
            process = run(args)
            result['versions'][name] = {
                'path': args[0], 'exit': process.returncode,
                'version': (process.stdout + process.stderr).splitlines()[0],
            }
        result['after'] = {unit: unit_state(unit) for unit in UNITS}
        result['production_units_unchanged'] = before == result['after']
        result['nginx_state'] = run([
            'systemctl', 'show', 'nginx',
            '--property=ActiveState,SubState,UnitFileState,MainPID',
        ]).stdout.strip()
        result['ports80_443_listening'] = listening_web_ports()
        if not result['production_units_unchanged'] or result['ports80_443_listening']:
            raise RuntimeError('production state or web listeners unexpectedly changed')
    except Exception as error:
        result['error_type'] = type(error).__name__
        result['error'] = str(error)
        raise
    finally:
        policy.unlink(missing_ok=True)
        if old_link is not None:
            policy.symlink_to(old_link)
        elif old_data is not None:
            policy.write_bytes(old_data)
            os.chmod(policy, old_mode)
        (base / 'infrastructure-install.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.release_dir)
