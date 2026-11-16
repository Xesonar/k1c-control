#!/usr/bin/env python3
"""Back up and install a staged K1C runtime. No service is stopped or started here.

The caller must explicitly stop only k1c-telegram before install/rollback. State
backups remain on this VPS. Rollback restores code/config/layout, never old data.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit


SOURCE = Path('/opt/k1c-telegram')
STATE = Path('/var/lib/k1c-telegram')
PRIVATE = Path('/var/lib/private/k1c-telegram')
CONFIG = Path('/etc/k1c-telegram/config.json')
DROPIN = Path('/etc/systemd/system/k1c-telegram.service.d/90-web-runtime.conf')
MEDIA = Path('/var/lib/k1c-media')
USER = 'k1c-runtime'
KEYS = ('access_control', 'queue', 'workflow')


def run(args):
    value = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if value.returncode:
        raise RuntimeError(f'{args[0]} failed ({value.returncode})')
    return value.stdout.strip()


def stopped():
    value = run(['systemctl', 'show', 'k1c-telegram', '--property=ActiveState', '--value'])
    if value not in ('inactive', 'failed'):
        raise RuntimeError('k1c-telegram must be explicitly stopped by caller')
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'k1c-media']).returncode == 0:
        raise RuntimeError('media worker must be stopped')


def state_hashes(db_path):
    with sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise RuntimeError('SQLite integrity check failed')
        values = {key: db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
                  for key in KEYS}
    return {key: hashlib.sha256((row[0] if row else '<missing>').encode()).hexdigest()
            for key, row in values.items()}


def backup_db(source, destination):
    with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite backup integrity check failed')
    os.chmod(destination, 0o600)


def write_json(path, value):
    temporary = path.with_suffix('.new')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def normalize_state(public=STATE, private=PRIVATE):
    """Only move the known DynamicUser state symlink; refuse unrelated targets."""
    if public.is_symlink():
        if public.resolve(strict=True) != private.resolve(strict=True) or not private.is_dir():
            raise RuntimeError('unexpected DynamicUser state link')
        public.unlink()
        try:
            os.replace(private, public)
        except BaseException:
            public.symlink_to(private)
            raise
        return True
    if not public.is_dir():
        raise RuntimeError('runtime state directory missing')
    return False


def restore_layout(was_symlink, public=STATE, private=PRIVATE):
    if not was_symlink or public.is_symlink():
        return
    if private.exists() or private.is_symlink():
        raise RuntimeError('private rollback target already exists')
    private.parent.mkdir(parents=True, exist_ok=True)
    os.replace(public, private)
    public.symlink_to(private)


def own_tree(root, uid, gid):
    for directory, folders, files in os.walk(root, followlinks=False):
        os.chown(directory, uid, gid, follow_symlinks=False)
        for name in folders + files:
            os.chown(Path(directory) / name, uid, gid, follow_symlinks=False)
    os.chmod(root, 0o700)


def account():
    try:
        user = pwd.getpwnam(USER)
    except KeyError:
        run(['useradd', '--system', '--user-group', '--no-create-home',
             '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', USER])
        user = pwd.getpwnam(USER)
    if user.pw_shell not in ('/usr/sbin/nologin', '/sbin/nologin', '/bin/false') or user.pw_uid == 0:
        raise RuntimeError('runtime account has unexpected privileges')
    if grp.getgrnam(USER).gr_gid != user.pw_gid:
        raise RuntimeError('runtime group mismatch')
    return user


def prepare(base):
    folder = base / ('before-runtime-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime()))
    folder.mkdir(mode=0o700)
    shutil.copytree(SOURCE, folder / 'source', symlinks=True)
    shutil.copy2(CONFIG, folder / 'config.json')
    os.chmod(folder / 'config.json', 0o600)
    backup_db(STATE / 'bot.sqlite3', folder / 'bot.sqlite3')
    if DROPIN.exists():
        shutil.copy2(DROPIN, folder / 'runtime-dropin.conf')
    for source, name in ((Path('/etc/systemd/system/k1c-media.service'), 'media-unit'),
                         (Path('/etc/k1c-media/config.json'), 'media-config')):
        if source.exists():
            shutil.copy2(source, folder / name)
    stat = STATE.stat()
    receipt = {'backup': str(folder), 'state_was_symlink': STATE.is_symlink(),
               'state_uid': stat.st_uid, 'state_gid': stat.st_gid,
               'hashes': state_hashes(folder / 'bot.sqlite3'), 'prepared': True}
    write_json(folder / 'receipt.json', receipt)
    return receipt


def install(base, backup, web_origin):
    origin = urlsplit(web_origin)
    if (origin.scheme != 'https' or not origin.hostname or origin.username is not None
            or origin.password is not None or origin.path not in ('', '/') or origin.query or origin.fragment):
        raise RuntimeError('provide a valid HTTPS web origin')
    web_origin = web_origin.rstrip('/')
    stopped()
    stage = base / 'stage'
    for name in ('bot.py', 'web_service.py', 'web_auth.py', 'media_service.py', 'media_worker.py'):
        if not (stage / name).is_file():
            raise RuntimeError('incomplete source stage')
    receipt = json.loads((backup / 'receipt.json').read_text())
    hashes = state_hashes(STATE / 'bot.sqlite3')
    backup_db(STATE / 'bot.sqlite3', backup / 'bot-at-stop.sqlite3')
    user = account()
    receipt['state_was_symlink'] = normalize_state()
    own_tree(STATE, user.pw_uid, user.pw_gid)
    MEDIA.mkdir(mode=0o700, exist_ok=True)
    own_tree(MEDIA, user.pw_uid, user.pw_gid)
    for item in stage.iterdir():
        if item.is_file() and (item.suffix == '.py' or item.name in ('README.txt', 'REVISION')):
            shutil.copy2(item, SOURCE / item.name)
    for name in ('web', 'deploy'):
        target = SOURCE / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(stage / name, target)
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(stage / 'deploy/k1c-telegram-web.conf', DROPIN)
    shutil.copy2(stage / 'deploy/k1c-media.service', '/etc/systemd/system/k1c-media.service')
    cfg = json.loads(CONFIG.read_text())
    cfg.update(web_enabled=True, web_host='127.0.0.1', web_port=3340,
               web_auth_db='/var/lib/k1c-telegram/web.sqlite3',
               web_static_dir='/opt/k1c-telegram/web', web_origin=web_origin,
               web_trust_proxy=True, media_enabled=True, media_state_dir=str(MEDIA))
    write_json(CONFIG, cfg)
    media_cfg = Path('/etc/k1c-media/config.json')
    media_cfg.parent.mkdir(mode=0o700, exist_ok=True)
    media_cfg.parent.chmod(0o700)
    os.chown(media_cfg.parent, user.pw_uid, user.pw_gid)
    media_value = json.loads((stage / 'deploy/media-config.example.json').read_text())
    media_value['media_capture_enabled'] = False
    write_json(media_cfg, media_value)
    os.chown(media_cfg, user.pw_uid, user.pw_gid)
    run(['systemctl', 'daemon-reload'])
    after = state_hashes(STATE / 'bot.sqlite3')
    if hashes != after:
        raise RuntimeError('protected state changed during install')
    receipt.update(at_stop_hashes=hashes, after_install_hashes=after,
                   runtime_uid=user.pw_uid, runtime_gid=user.pw_gid, installed=True)
    write_json(backup / 'receipt.json', receipt)
    return {'installed': True, 'service_started': False, 'capture_enabled': False,
            'protected_hashes_equal': True, 'runtime_uid': user.pw_uid, 'backup': str(backup)}


def rollback(backup):
    stopped()
    receipt = json.loads((backup / 'receipt.json').read_text())
    before = state_hashes(STATE / 'bot.sqlite3')
    for item in SOURCE.iterdir():
        if item.name in ('__pycache__',):
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
    shutil.copytree(backup / 'source', SOURCE, dirs_exist_ok=True, symlinks=True)
    shutil.copy2(backup / 'config.json', CONFIG)
    if (backup / 'runtime-dropin.conf').exists():
        shutil.copy2(backup / 'runtime-dropin.conf', DROPIN)
    else:
        DROPIN.unlink(missing_ok=True)
    for target, name in ((Path('/etc/systemd/system/k1c-media.service'), 'media-unit'),
                         (Path('/etc/k1c-media/config.json'), 'media-config')):
        if (backup / name).exists():
            shutil.copy2(backup / name, target)
        else:
            target.unlink(missing_ok=True)
    own_tree(STATE, receipt['state_uid'], receipt['state_gid'])
    restore_layout(receipt['state_was_symlink'])
    run(['systemctl', 'daemon-reload'])
    if before != state_hashes(STATE / 'bot.sqlite3'):
        raise RuntimeError('rollback changed protected data')
    return {'rolled_back': True, 'service_started': False, 'state_preserved': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'install', 'rollback'))
    parser.add_argument('--release-dir', type=Path, required=True)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--web-origin', help='Required for install, e.g. https://dashboard.example.com')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('root required on authorized VPS')
    base = args.release_dir.resolve()
    if not base.is_dir() or not str(base).startswith('/root/k1c-web-release-'):
        raise SystemExit('existing release directory required')
    if args.phase == 'prepare':
        result = prepare(base)
    else:
        backup = args.backup.resolve() if args.backup else None
        if not backup or backup.parent != base or not (backup / 'receipt.json').exists():
            raise SystemExit('specific prepared backup required')
        if args.phase == 'install' and not args.web_origin:
            raise SystemExit('--web-origin is required for install')
        result = install(base, backup, args.web_origin) if args.phase == 'install' else rollback(backup)
    print(json.dumps(result))
