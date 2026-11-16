#!/usr/bin/env python3
"""Entrypoint for the private K1C Telegram manager."""
import argparse
import json
import logging
import math
import queue
import re
import signal
import threading
import time
import urllib.parse
from pathlib import Path

from adapters import ApiError, Printer, Telegram, Slicer
from detector import Detector
from manager import Manager, authorized
from storage import Store
from configuration import ConfigurationError, validate_config

LOG = logging.getLogger('k1c-telegram')


class UpdateDispatcher:
    """Bounded camera isolation, preserving the order of all owner actions.

    Updates are durably consumed by the polling thread in Telegram order,
    before dispatch. This preserves the existing at-most-once control policy:
    a process restart never replays a queued physical action.
    """
    def __init__(self, store, manager, capacity=24):
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError('invalid_update_queue_capacity')
        self.store, self.manager = store, manager
        self.closed = threading.Event()
        self.capacity = threading.BoundedSemaphore(capacity)
        self.queues = {lane: queue.Queue() for lane in ('owner', 'photo')}
        self.workers = [threading.Thread(target=self._work, args=(lane, pending),
                       name='k1c-' + str(lane), daemon=True)
                       for lane, pending in self.queues.items()]
        for worker in self.workers:
            worker.start()

    def _private_sender(self, message):
        sender = message.get('from', {}).get('id')
        if hasattr(self.manager, 'access'):
            return type(sender) is int and sender > 0 and authorized(message, {sender})
        # Minimal dispatcher fakes retain their legacy explicit allowlist.
        return authorized(message, self.manager.owners)

    def _lane(self, update):
        callback = update.get('callback_query')
        message = update.get('message', {})
        if isinstance(callback, dict):
            check = {'from': callback.get('from', {}), 'chat': callback.get('message', {}).get('chat', {})}
            if not self._private_sender(check):
                return None
            return 'photo' if callback.get('data') == 'n:photo' else 'owner'
        if not self._private_sender(message):
            return None
        text = message.get('text', '').strip()
        command = text.split()[0].split('@')[0].lower() if text else ''
        # Only the pure camera query bypasses the ordered owner action lane.
        # Documents and every other slash command retain their original order.
        is_video = re.search(r'видео|ролик|клип', text.lower())
        if not is_video and 'document' not in message and (command == '/photo' or
                (not command.startswith('/') and re.search(r'фото|камер|снимок', text.lower()))):
            return 'photo'
        return 'owner'

    def submit(self, update):
        lane = self._lane(update)
        if lane is None:
            # Consume disallowed updates without allocating a worker or queue slot.
            self.store.consume_update(update['update_id'])
            return False
        while not self.closed.is_set() and not self.manager.stopping:
            if not self.capacity.acquire(timeout=.1):
                continue
            if self.closed.is_set() or self.manager.stopping:
                self.capacity.release()
                return False
            try:
                if not self.store.consume_update(update['update_id']):
                    self.capacity.release()
                    return False
                self.queues[lane].put_nowait(update)
            except BaseException:
                self.capacity.release()
                raise
            return True
        return False

    def _work(self, lane, pending):
        while True:
            try:
                update = pending.get(timeout=.1)
            except queue.Empty:
                if self.closed.is_set():
                    return
                continue
            try:
                if not self.closed.is_set() and not self.manager.stopping:
                    started = time.monotonic()
                    try:
                        self.manager.handle_update(update, consumed=True)
                    except Exception as exc:
                        # URLs, callback data and credentials never enter the log.
                        LOG.error('update failure type=%s', type(exc).__name__)
                    elapsed = time.monotonic() - started
                    if elapsed >= 2:
                        LOG.info('slow request kind=%s elapsed_ms=%s',
                                 'photo' if lane == 'photo' else 'owner', round(elapsed * 1000))
            finally:
                self.capacity.release()
                pending.task_done()

    def close(self, timeout=60):
        self.closed.set()
        deadline = time.monotonic() + timeout
        for worker in self.workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in self.workers):
            LOG.warning('update workers exceeded shutdown grace')


def migrate(store, directory, owners):
    if store.get('schema_version') is not None:
        return
    legacy = Path(directory) / 'state.json'
    if legacy.exists():
        data = json.loads(legacy.read_text())
        offset = data.get('offset', 0)
        if type(offset) is int and offset >= 0:
            store.set('offset', offset)
        store.set('peers', sorted(set(data.get('subscribers', [])) & set(owners)))
    store.set('schema_version', 2)


def start_interfaces(config, manager):
    """Create optional local web/media clients; this never starts capture."""
    for name in ('web_enabled', 'media_enabled'):
        if type(config.get(name, False)) is not bool:
            raise ValueError('invalid_' + name)
    media, web = None, None
    try:
        if config.get('media_enabled', False):
            from media_service import MediaClient
            media = MediaClient(config)
        manager.media_client = media
        if config.get('web_enabled', False):
            from web_service import WebServer
            web = WebServer(manager, config, media=media)
            web.start()
        return media, web
    except BaseException:
        if web is not None:
            web.close()
        if media is not None:
            media.close()
        manager.media_client = None
        raise


def _archive_link(config):
    origin = config.get('web_origin', '').rstrip('/')
    try:
        parsed = urllib.parse.urlsplit(origin)
        valid = (parsed.scheme == 'https' and parsed.hostname and parsed.username is None
                 and parsed.password is None and parsed.path == '' and not parsed.query
                 and not parsed.fragment and all(ord(char) > 32 for char in origin))
    except ValueError:
        valid = False
    if not valid:
        raise ApiError('web_link_unavailable')
    return origin + '/#media'


def process_media_deliveries(manager, telegram, media_client, config, *, limit=1):
    """Deliver explicitly requested clips once; durable claims are never replayed."""
    rows = media_client.pending_deliveries(limit=limit)
    processed = 0
    for row in rows:
        request_id, uid = row['id'], row['uid']
        attempted, outcome = False, 'failed'
        try:
            allowed = lambda: (not manager.stopping and manager.access.is_allowed(uid))
            if not allowed():
                outcome = 'revoked'
                continue
            if row.get('status') in {'cancelled', 'revoked'}:
                outcome = 'revoked'
                continue
            if row.get('status') == 'failed':
                message = {
                    'buffer_not_ready': 'Не удалось собрать фрагмент: нужного интервала нет в записи.',
                    'archive_full': 'В видеоархиве закончилось место. Владелец может удалить старые записи.',
                    'ffmpeg_failed': 'Не удалось подготовить ролик. Файл не отправлен.',
                }.get(row.get('error'), 'Не удалось подготовить запрошенный ролик. Файл не отправлен.')
                if not allowed():
                    outcome = 'revoked'
                    continue
                attempted = True
                telegram.send_message(uid, message)
                outcome = 'sent'
                continue
            media = media_client.download(uid, row['media_id'])
            if not allowed():
                outcome = 'revoked'
                continue
            size = media.get('size')
            if type(size) is not int or size < 1:
                raise ApiError('invalid_video_file')
            caption = '🎞 Запрошенный фрагмент печати'
            duration = row.get('actual_duration')
            if type(duration) in (int, float) and math.isfinite(duration) and duration > 0:
                caption += ' · ' + str(round(duration, 1)) + ' с'
            else:
                duration = None
            missing = row.get('missing_seconds')
            if type(missing) in (int, float) and math.isfinite(missing) and missing > 0:
                caption += '\nВ записи есть пропуски: ' + str(round(missing, 1)) + ' с.'
            if size > Telegram.MAX_VIDEO_BYTES:
                link = _archive_link(config)
                if not allowed():
                    outcome = 'revoked'
                    continue
                attempted = True
                telegram.send_message(uid, caption + '\nРолик больше лимита Telegram. Открой архив: ' + link)
                outcome = 'link'
            else:
                attempted = True
                telegram.send_video(uid, caption, media['path'], duration=duration, authorize=allowed)
                outcome = 'sent'
        except ApiError as exc:
            if exc.code == 'access_revoked':
                outcome = 'revoked'
            elif attempted and (exc.uncertain or exc.code in {
                    'network', 'incomplete_response', 'invalid_json', 'invalid_envelope', 'video_file_changed'
                    } or (exc.http_status is not None and exc.http_status >= 500)):
                outcome = 'unknown'
            else:
                outcome = 'failed'
            LOG.info('media delivery unavailable reason=%s', exc.code)
        except Exception as exc:
            outcome = 'unknown' if attempted else 'failed'
            LOG.warning('media delivery failure type=%s', type(exc).__name__)
        finally:
            try:
                media_client.complete_delivery(request_id, outcome)
            except Exception as exc:
                # It remains durably claimed, never an automatically replayable send.
                LOG.warning('media delivery completion failure type=%s', type(exc).__name__)
            processed += 1
    return processed


def maintain_media_access(manager, media_client):
    """Cancel revoked jobs and deduplicate one owner-only quota warning."""
    media_client.cancel_disallowed(manager.access.is_allowed)
    state = media_client.status()
    sequence = state.get('quota_warning_sequence')
    if state.get('quota_paused') is not True or type(sequence) is not int or sequence < 1:
        return
    previous = manager.store.get('media_quota_warning_sequence', 0)
    if type(previous) is int and sequence <= previous:
        return
    manager.store.enqueue('media-quota:' + str(sequence), [manager.access.owner_id],
                          '⚠️ Видеоархив заполнен. Новые записи временно остановлены. '
                          'Удали ненужные записи в архиве сайта, чтобы освободить место.')
    manager.store.set('media_quota_warning_sequence', sequence)


def run(config, store, manager, telegram):
    identity = telegram.call('getMe')
    if telegram.call('getWebhookInfo').get('url'):
        raise RuntimeError('existing_webhook')
    LOG.info('manager connected username=%s owners=%s', identity.get('username'), len(manager.owners))
    manager.enable_fast_ui()
    try:
        media_client, web = start_interfaces(config, manager)
    except BaseException:
        manager.close_fast_ui()
        raise
    stop = threading.Event()

    def shutdown(*_):
        manager.stopping = True
        manager.close_fast_ui()
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)

    def monitor():
        while not stop.is_set():
            try:
                manager.tick()
            except Exception as exc:
                LOG.error('monitor failure type=%s', type(exc).__name__)
            stop.wait(config.get('poll_interval', 10))

    def watchdog():
        while not stop.is_set():
            try:
                manager.check_deadlines()
            except Exception as exc:
                LOG.error('watchdog failure type=%s', type(exc).__name__)
            stop.wait(1)

    def deliveries():
        while not stop.is_set():
            try:
                manager.flush_outbox()
            except Exception as exc:
                LOG.error('delivery failure type=%s', type(exc).__name__)
            stop.wait(1)

    def slicing():
        while not stop.is_set():
            manager.slicing_wakeup.clear()
            try:
                processed = manager.process_slicing_job()
            except Exception as exc:
                LOG.error('slicing worker failure type=%s', type(exc).__name__)
                processed = False
            if not processed:
                manager.slicing_wakeup.wait(1)

    def preflight():
        while not stop.is_set():
            try:manager.preflight.tick()
            except Exception as exc:LOG.error('preflight worker failure type=%s',type(exc).__name__)
            stop.wait(.5)

    def panels():
        while not stop.is_set():
            manager.panels.wakeup.clear()
            try:
                manager.refresh_panels()
                manager.panels.flush_pending()
            except Exception as exc:LOG.error('panel worker failure type=%s',type(exc).__name__)
            manager.panels.wakeup.wait(1)

    def media_deliveries():
        while not stop.is_set():
            try:
                maintain_media_access(manager, media_client)
                process_media_deliveries(manager, telegram, media_client, config)
            except Exception as exc:
                LOG.warning('media delivery worker failure type=%s', type(exc).__name__)
            stop.wait(1)

    targets = [monitor, watchdog, deliveries, slicing, preflight, panels]
    if media_client is not None:
        targets.append(media_deliveries)
    workers = [threading.Thread(target=target, daemon=True) for target in targets]
    dispatcher = None
    try:
        for worker in workers:
            worker.start()
        dispatcher = UpdateDispatcher(store, manager, config.get('update_queue_capacity', 24))
        while not stop.is_set():
            try:
                updates = telegram.get_updates(store.get('offset', 0), timeout=15)
                for update in updates:
                    if stop.is_set():
                        break
                    dispatcher.submit(update)
            except ApiError as exc:
                LOG.warning('telegram unavailable reason=%s', exc.code)
                stop.wait(exc.retry_after or 5)
    finally:
        shutdown()
        if web is not None:
            try:
                web.close()
            except Exception as exc:
                LOG.warning('web shutdown failure type=%s', type(exc).__name__)
        if dispatcher is not None:
            dispatcher.close()
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=60)
        if media_client is not None:
            try:
                media_client.close()
            except Exception as exc:
                LOG.warning('media shutdown failure type=%s', type(exc).__name__)
        manager.media_client = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--state-dir', default='/var/lib/k1c-telegram')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--check-config', action='store_true', help='Validate local settings without networking or creating state')
    parser.add_argument('--test-notify', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = validate_config(json.loads(Path(args.config).read_text()))
    if args.check_config:
        print(json.dumps({'ok': True, 'configuration': 'valid'}))
        return
    config['state_dir'] = args.state_dir
    store = Store(Path(args.state_dir) / 'bot.sqlite3')
    try:
        migrate(store, args.state_dir, config.get('allowed_user_ids', []))
        telegram, printer, detector = Telegram(config), Printer(config), Detector(config)
        manager = Manager(config, store, printer, telegram, detector=detector, slicer=Slicer(config))
        if args.check:
            result = {'telegram': telegram.call('getMe')['username'], 'owners': sorted(manager.owners), 'users': sorted(manager.access.allowed_users()), 'detector': detector.status(), 'power': manager.power.status()}
            try:
                result['printer'] = manager.context()['state']
            except ApiError:
                result['printer'] = 'unavailable'
            print(json.dumps(result, ensure_ascii=False))
        elif args.test_notify:
            manager.notify('🧪 Проверка нового менеджера K1C. Печать и движение не запускались. Открой /start, чтобы обновить меню.')
            manager.flush_outbox()
            print('test notification queued for approved users')
        else:
            run(config, store, manager, telegram)
    finally:
        store.close()


if __name__ == '__main__':
    try:
        main()
    except ConfigurationError as exc:
        LOG.error('configuration error: %s', exc)
        raise SystemExit(1)
    except Exception as exc:
        LOG.error('startup failure type=%s', type(exc).__name__)
        raise SystemExit(1)
