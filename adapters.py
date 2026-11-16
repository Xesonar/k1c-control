"""Bounded HTTP adapters for Moonraker and Telegram. Python 3.9+, stdlib only.

API errors intentionally retain neither response bodies nor request URLs, since
Telegram includes its credential in URL paths. Physical printer POSTs are never
retried here: a transport failure cannot establish whether a move was executed.
"""
import json
import hashlib
import http.client
import math
import os
import re
import socket
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class ApiError(Exception):
    def __init__(self, code="api_error", uncertain=False, http_status=None, retry_after=None):
        self.code = code if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(code)) else "api_error"
        self.uncertain = bool(uncertain)
        self.http_status = http_status
        self.retry_after = retry_after
        super().__init__(self.code)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())


def _read(response, max_bytes):
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) < 0:
                raise ApiError("invalid_content_length")
            if int(declared) > max_bytes:
                raise ApiError("response_too_large")
        except ValueError:
            raise ApiError("invalid_content_length") from None
    data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ApiError("response_too_large")
    if declared is not None and len(data) != int(declared):
        raise ApiError("incomplete_response")
    return data


def _response_error(exc, physical=False):
    retry_after = None
    not_modified = False
    try:
        details = json.loads(exc.read(4096))
        candidate = details.get("parameters", {}).get("retry_after") if isinstance(details, dict) else None
        if type(candidate) is int and candidate >= 0:
            retry_after = candidate
        not_modified = (exc.code == 400 and isinstance(details, dict)
                        and isinstance(details.get("description"), str)
                        and "message is not modified" in details["description"].lower())
    except (ValueError, OSError, AttributeError, http.client.HTTPException):
        pass
    finally:
        exc.close()
    return ApiError("message_not_modified" if not_modified else "http_" + str(exc.code), uncertain=physical and (exc.code >= 500 or exc.code == 408 or exc.code < 400), http_status=exc.code, retry_after=retry_after)


def _http(url, payload=None, body=None, headers=None, timeout=12, physical=False, max_bytes=10 * 1024 * 1024):
    if payload is not None:
        body = json.dumps(payload).encode()
        headers = dict(headers or {}, **{"Content-Type": "application/json"})
    request = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with _opener().open(request, timeout=timeout) as response:
            return _read(response, max_bytes)
    except urllib.error.HTTPError as exc:
        raise _response_error(exc, physical) from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise ApiError("network", uncertain=physical) from None
    except ApiError as exc:
        if physical:
            exc.uncertain = True
        raise


def _get(url, timeout=12, max_bytes=10 * 1024 * 1024, validate=None):
    """Retry one transient read failure inside the original request budget.

    Only used for idempotent reads. Mutations (including Telegram sends) keep
    the one-attempt transport, since a lost reply cannot establish success.
    """
    deadline = time.monotonic() + timeout
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ApiError("network")
        try:
            raw = _http(url, timeout=remaining, max_bytes=max_bytes)
            return validate(raw) if validate is not None else raw
        except ApiError as exc:
            transient = exc.code in {"network", "incomplete_response", "invalid_jpeg", "http_408", "http_500", "http_502", "http_503", "http_504"}
            if attempt or not transient:
                raise


def _json(url, payload=None, timeout=12, physical=False, body=None, headers=None, retry_read=False):
    if retry_read and payload is None and body is None and not physical and not headers:
        raw = _get(url, timeout=timeout)
    else:
        raw = _http(url, payload=payload, timeout=timeout, physical=physical, body=body, headers=headers)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ApiError("invalid_json", uncertain=physical) from None
    if not isinstance(result, dict):
        raise ApiError("invalid_envelope", uncertain=physical)
    return result


def _filename(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ApiError("unsafe_filename")
    if value.startswith("/") or "\\" in value or any(ord(char) < 32 for char in value) or ":" in value:
        raise ApiError("unsafe_filename")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ApiError("unsafe_filename")
    decoded = urllib.parse.unquote(value)
    if (decoded.count("/") != value.count("/") or "\\" in decoded
            or any(ord(char) < 32 for char in decoded)
            or any(part in {".", ".."} for part in decoded.split("/"))):
        raise ApiError("unsafe_filename")
    return value


def _limit(value, maximum=100):
    if type(value) is not int or value < 1 or value > maximum:
        raise ApiError("invalid_limit")
    return value


def _multipart(fields, file_field, filename, content, content_type):
    boundary = "k1c" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode())
    header_name = filename.replace('"', '\\"')
    chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{header_name}\"\r\nContent-Type: {content_type}\r\n\r\n").encode())
    chunks.extend([content, f"\r\n--{boundary}--\r\n".encode()])
    return b"".join(chunks), {"Content-Type": "multipart/form-data; boundary=" + boundary}


def _multipart_file(fields, file_field, filename, source, size, digest, progress=None,
                    content_type="application/octet-stream"):
    """Known-length multipart stream; at most one 64 KiB file chunk in memory."""
    boundary = "k1c" + uuid.uuid4().hex
    prefix = []
    for name, value in fields.items():
        prefix.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode())
    quoted = filename.replace('"', '\\"')
    prefix.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{quoted}\"\r\nContent-Type: {content_type}\r\n\r\n").encode())
    prefix = b"".join(prefix)
    suffix = f"\r\n--{boundary}--\r\n".encode()

    def chunks():
        def report(sent):
            if progress is not None:
                try:
                    progress(sent, size)
                except Exception:
                    # A failed Telegram progress edit must not corrupt upload.
                    pass
        report(0)
        yield prefix
        remaining = size
        while remaining:
            data = source.read(min(65536, remaining))
            if not data:
                raise ApiError("upload_file_changed")
            digest.update(data)
            remaining -= len(data)
            yield data
            report(size - remaining)
        yield suffix

    return chunks(), {"Content-Type": "multipart/form-data; boundary=" + boundary,
                      "Content-Length": str(len(prefix) + size + len(suffix))}


def _jpeg(raw):
    if not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
        raise ApiError("invalid_jpeg")
    return raw


def _temperature(heater, target):
    maximum = {"extruder": 300, "heater_bed": 100}.get(heater)
    if maximum is None or type(target) not in {int, float} or not math.isfinite(target) or not 0 <= target <= maximum:
        raise ApiError("invalid_temperature")
    return format(target, ".12g")


class Printer:
    def __init__(self, config):
        self.url = config["printer_url"].rstrip("/")
        self.snapshot_url = config["snapshot_url"]
        self.timeout = config.get("request_timeout", 12)
        self.upload_directory = config.get("upload_directory", "telegram")
        if self.upload_directory:
            _filename(self.upload_directory)
        self.max_upload_bytes = config.get("max_upload_bytes", 128 * 1024 * 1024)
        host = urllib.parse.urlsplit(self.url).hostname
        authority = "[" + host + "]" if host and ":" in host else host
        self.light_ws_url = config.get("light_ws_url", "ws://" + (authority or "invalid") + ":9999/")
        self.light_timeout = config.get("light_timeout", min(self.timeout, 6))

    def _request(self, path, payload=None, physical=False, timeout=None):
        raw = _json(self.url + path, payload=payload, timeout=self.timeout if timeout is None else timeout, physical=physical, retry_read=payload is None and not physical)
        if "error" in raw:
            raise ApiError("moonraker_rejected")
        if "result" not in raw:
            raise ApiError("invalid_envelope", uncertain=physical)
        if physical and raw["result"] != "ok":
            raise ApiError("invalid_command_response", uncertain=True)
        return raw["result"]

    def status(self):
        result = self._request("/printer/objects/query?webhooks&print_stats&virtual_sdcard&extruder&heater_bed&bed_mesh&pause_resume")
        if not isinstance(result, dict) or not isinstance(result.get("status"), dict):
            raise ApiError("invalid_status")
        return result["status"]

    def history(self, limit=10):
        query = urllib.parse.urlencode({"limit": _limit(limit), "order": "desc"})
        result = self._request("/server/history/list?" + query)
        if not isinstance(result, dict) or not isinstance(result.get("jobs"), list) or not all(isinstance(job, dict) for job in result["jobs"]):
            raise ApiError("invalid_history")
        return result["jobs"]

    def active_job(self):
        return next((job for job in self.history() if job.get("status") == "in_progress"), None)

    def files(self, limit=12):
        _limit(limit, maximum=1000)
        result = self._request("/server/files/list?root=gcodes")
        if not isinstance(result, list) or not all(isinstance(item, dict) and isinstance(item.get("path"), str) for item in result):
            raise ApiError("invalid_files")
        try:
            return sorted(result, key=lambda item: float(item.get("modified", 0)), reverse=True)[:limit]
        except (ValueError, TypeError):
            raise ApiError("invalid_files") from None

    def metadata(self, filename):
        query = urllib.parse.urlencode({"filename": _filename(filename)})
        result = self._request("/server/files/metadata?" + query)
        if not isinstance(result, dict):
            raise ApiError("invalid_metadata")
        return result

    def photo(self):
        return _get(self.snapshot_url, timeout=self.timeout, validate=_jpeg)

    def light_status(self):
        # Imported lazily: the narrow transport shares ApiError with adapters.
        from light import CrealityLight
        return CrealityLight(self.light_ws_url, self.light_timeout).status()

    def set_light(self, on):
        from light import CrealityLight
        return CrealityLight(self.light_ws_url, self.light_timeout).set(on)

    def upload(self, filename, path, progress=None):
        _filename(filename)
        if "/" in filename or Path(filename).suffix.lower() not in {".gcode", ".g", ".gco", ".ufp", ".nc"}:
            raise ApiError("unsafe_filename")
        source = Path(path)
        if progress is not None and not callable(progress):
            raise ApiError("invalid_upload_progress")
        try:
            if not source.is_file() or source.is_symlink():
                raise ApiError("invalid_upload_file")
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ApiError("invalid_upload_file")
                if info.st_size > self.max_upload_bytes:
                    raise ApiError("upload_too_large")
                checksum = hashlib.sha256()
                remaining = info.st_size
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        raise ApiError("upload_file_changed")
                    checksum.update(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    raise ApiError("upload_file_changed")
                hashed = os.fstat(stream.fileno())
                signature = lambda value: (value.st_size, value.st_mtime_ns, value.st_ctime_ns)
                if signature(info) != signature(hashed):
                    raise ApiError("upload_file_changed")
                stream.seek(0)
                fields = {"root": "gcodes", "path": self.upload_directory, "print": "false", "checksum": checksum.hexdigest()}
                sent_digest = hashlib.sha256()
                body, headers = _multipart_file(fields, "file", filename, stream, info.st_size, sent_digest, progress)
                raw = _json(self.url + "/server/files/upload", body=body, headers=headers, timeout=max(self.timeout, 60))
                if signature(info) != signature(os.fstat(stream.fileno())) or sent_digest.digest() != checksum.digest():
                    raise ApiError("upload_file_changed")
        except OSError:
            raise ApiError("upload_read_failed") from None
        # Upload uses a dedicated handler, not the usual wrapped JSON endpoint.
        result = raw.get("result", raw)
        if "error" in raw:
            raise ApiError("moonraker_rejected")
        if not isinstance(result, dict) or not isinstance(result.get("item"), dict):
            raise ApiError("invalid_upload_response")
        if result.get("print_started") or result.get("print_queued"):
            raise ApiError("unexpected_print_start", uncertain=True)
        item = result["item"]
        if item.get("root") != "gcodes":
            raise ApiError("invalid_upload_root")
        return _filename(item.get("path"))

    def start(self, filename):
        return self._request("/printer/print/start", {"filename": _filename(filename)}, physical=True)

    def start_calibrated(self, filename):
        name = _filename(filename).replace('"', '\\"')
        return self._request("/printer/gcode/script", {"script": f'SDCARD_PRINT_FILE FILENAME="{name}" NEEDCALIBRATE=1'}, physical=True)

    def set_temperature(self, heater, target):
        value = _temperature(heater, target)
        return self._request("/printer/gcode/script", {"script": f"SET_HEATER_TEMPERATURE HEATER={heater} TARGET={value}"}, physical=True)

    def recalibrate(self, bed, nozzle):
        bed, nozzle = _temperature("heater_bed", bed), _temperature("extruder", nozzle)
        script = f"M190 S{bed}\nM109 S140\nBED_MESH_CALIBRATE ADAPTIVE=1\nSAVE_CONFIG_NORESTART\nM109 S{nozzle}"
        return self._request("/printer/gcode/script", {"script": script}, physical=True, timeout=900)

    def resume_gate(self, gate):
        if (not isinstance(gate, dict) or not isinstance(gate.get("token"), str)
                or not re.fullmatch(r"[A-Za-z0-9_]{8,64}", gate["token"])
                or gate.get("state_name") != "K1C_GATE_" + gate["token"]):
            raise ApiError("invalid_gate")
        script = "RESTORE_GCODE_STATE NAME=" + gate["state_name"] + " MOVE=1\nM24"
        return self._request("/printer/gcode/script", {"script": script}, physical=True)

    def download_gcode(self, filename, destination):
        filename = _filename(filename)
        target = Path(destination)
        temporary = target.with_name(target.name + ".part-" + uuid.uuid4().hex)
        maximum = 128 * 1024 * 1024
        url = self.url + "/server/files/gcodes/" + urllib.parse.quote(filename, safe="/")
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with _opener().open(url, timeout=max(60, self.timeout)) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None and not re.fullmatch(r"[0-9]+", declared):
                    raise ApiError("gcode_invalid_length")
                size = int(declared) if declared is not None else None
                if size is not None and size > maximum:
                    raise ApiError("gcode_too_large")
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    total = 0
                    while size is None or total < size:
                        chunk = response.read1(min(65536, maximum - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > maximum:
                            raise ApiError("gcode_too_large")
                        output.write(chunk)
                    if not total or (size is not None and total != size):
                        raise ApiError("gcode_incomplete_download")
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(temporary, target)
            return target
        except urllib.error.HTTPError as exc:
            raise _response_error(exc) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise ApiError("gcode_download_failed") from None
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def pause(self):
        return self._request("/printer/print/pause", {}, physical=True)

    def resume(self):
        return self._request("/printer/print/resume", {}, physical=True)

    def cancel(self):
        return self._request("/printer/print/cancel", {}, physical=True)


class Telegram:
    MAX_VIDEO_BYTES = 50_000_000
    def __init__(self, config):
        self.token = config["bot_token"]
        self.base = config.get("telegram_api_base", "https://api.telegram.org").rstrip("/")

    def _method_url(self, method):
        if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", method):
            raise ApiError("invalid_method")
        return self.base + "/bot" + self.token + "/" + method

    def _result(self, response):
        if response.get("ok") is not True:
            error_code = response.get("error_code")
            if type(error_code) is not int:
                error_code = None
            params = response.get("parameters", {})
            retry_after = params.get("retry_after") if isinstance(params, dict) else None
            if type(retry_after) is not int or retry_after < 0:
                retry_after = None
            if (error_code == 400 and isinstance(response.get("description"), str)
                    and "message is not modified" in response["description"].lower()):
                raise ApiError("message_not_modified", http_status=400)
            raise ApiError("telegram_" + str(error_code) if error_code else "telegram_rejected", http_status=error_code, retry_after=retry_after)
        if "result" not in response:
            raise ApiError("invalid_envelope")
        return response["result"]

    def call(self, method, payload=None, timeout=12):
        return self._result(_json(self._method_url(method), payload=payload, timeout=timeout))

    def get_updates(self, offset, timeout=20):
        if type(offset) is not int or offset < 0 or type(timeout) is not int or not 0 <= timeout <= 50:
            raise ApiError("invalid_poll")
        result = self.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}, timeout=max(12, timeout + 10))
        if not isinstance(result, list):
            raise ApiError("invalid_updates")
        return result

    def send_message(self, user, text, markup=None):
        payload = {"chat_id": user, "text": str(text)[:4000]}
        if markup is not None:
            payload["reply_markup"] = markup
        return self.call("sendMessage", payload)

    def send_photo(self, user, caption, photo, markup=None):
        if not isinstance(photo, bytes) or len(photo) > 10 * 1024 * 1024:
            raise ApiError("invalid_photo")
        _jpeg(photo)
        fields = {"chat_id": user, "caption": str(caption)[:1000]}
        if markup is not None:
            fields["reply_markup"] = json.dumps(markup, ensure_ascii=False)
        body, headers = _multipart(fields, "photo", "k1c.jpg", photo, "image/jpeg")
        return self._result(_json(self._method_url("sendPhoto"), body=body, headers=headers, timeout=20))

    def send_video(self, user, caption, path, *, duration=None, markup=None, authorize=None):
        """One known-length MP4 upload, bounded to64KiB chunks; never retries.

        ``authorize`` is rechecked before every upload chunk so revocation can
        stop an in-flight transfer. An uncertain send is terminal to the caller.
        """
        if type(user) is not int or user <= 0:
            raise ApiError("invalid_recipient")
        if authorize is not None and not callable(authorize):
            raise ApiError("invalid_authorizer")
        if duration is not None and (type(duration) not in (int, float)
                or not math.isfinite(duration) or not 0 < duration <= 86400):
            raise ApiError("invalid_video_duration")
        fields = {"chat_id": user, "caption": str(caption)[:1000], "supports_streaming": "true"}
        if duration is not None:
            fields["duration"] = str(math.ceil(duration))
        if markup is not None:
            fields["reply_markup"] = json.dumps(markup, ensure_ascii=False)
        try:
            source = Path(path)
        except (TypeError, ValueError):
            raise ApiError("invalid_video_file") from None
        try:
            if source.is_symlink():
                raise ApiError("invalid_video_file")
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size < 1:
                    raise ApiError("invalid_video_file")
                if info.st_size > self.MAX_VIDEO_BYTES:
                    raise ApiError("video_too_large")
                body, headers = _multipart_file(fields, "video", "k1c-clip.mp4", stream,
                                               info.st_size, hashlib.sha256(), content_type="video/mp4")

                def guarded_chunks():
                    for chunk in body:
                        if authorize is not None and not authorize():
                            raise ApiError("access_revoked")
                        yield chunk

                # The transport's physical flag means a non-idempotent operation:
                # losing its reply cannot establish whether Telegram sent a video.
                response = _json(self._method_url("sendVideo"), body=guarded_chunks(),
                                 headers=headers, timeout=120, physical=True)
                current = os.fstat(stream.fileno())
                signature = lambda value: (value.st_size, value.st_mtime_ns, value.st_ctime_ns)
                if signature(info) != signature(current):
                    raise ApiError("video_file_changed", uncertain=True)
                return self._result(response)
        except OSError:
            raise ApiError("video_read_failed") from None

    def answer_callback(self, callback_id, text=""):
        # A UI acknowledgement must not hold up the requested printer action.
        return self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": str(text)[:200]}, timeout=3)

    def edit_message(self, user, message_id, text, markup=None):
        payload = {"chat_id": user, "message_id": message_id, "text": str(text)[:4000]}
        if markup is not None:
            payload["reply_markup"] = markup
        try:
            return self.call("editMessageText", payload)
        except ApiError as exc:
            if exc.code == "message_not_modified":
                return True
            raise

    def edit_message_reply_markup(self, user, message_id, markup=None):
        try:
            return self.call("editMessageReplyMarkup", {"chat_id": user, "message_id": message_id,
                                                       "reply_markup": markup or {"inline_keyboard": []}})
        except ApiError as exc:
            if exc.code == "message_not_modified":
                return True
            raise

    def download_file(self, file_id, destination, max_bytes=20 * 1024 * 1024):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ApiError("invalid_download_limit")
        info = self.call("getFile", {"file_id": file_id})
        if not isinstance(info, dict):
            raise ApiError("invalid_file")
        size = info.get("file_size")
        if size is not None and (type(size) is not int or size < 0):
            raise ApiError("invalid_file_size")
        if size is not None and size > max_bytes:
            raise ApiError("download_too_large")
        file_path = _filename(info.get("file_path"))
        url = self.base + "/file/bot" + self.token + "/" + urllib.parse.quote(file_path, safe="/")
        target = Path(destination)
        temporary = target.with_name(target.name + ".part-" + uuid.uuid4().hex)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with _opener().open(url, timeout=60) as response:
                declared = response.headers.get("Content-Length")
                try:
                    declared_size = int(declared) if declared is not None else None
                except ValueError:
                    raise ApiError("invalid_content_length") from None
                if declared_size is not None and declared_size < 0:
                    raise ApiError("invalid_content_length")
                if declared_size is not None and declared_size > max_bytes:
                    raise ApiError("download_too_large")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out:
                    total = 0
                    while True:
                        chunk = response.read(min(65536, max_bytes - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ApiError("download_too_large")
                        out.write(chunk)
                    if (declared_size is not None and declared_size != total) or (size is not None and size != total):
                        raise ApiError("incomplete_download")
                    out.flush()
                    os.fsync(out.fileno())
            os.replace(temporary, target)
            return target
        except urllib.error.HTTPError as exc:
            raise _response_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise ApiError("download_failed") from None
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class Slicer:
    """Private STL -> PETG service client; no command or URL comes from an STL.

    The POST is never retried. Both input and output are streamed with a shared
    wall-clock deadline; a partial result never replaces an existing G-code.
    """
    def __init__(self, config):
        self.base = config.get("slicer_url", "http://127.0.0.1:3334").rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        try:
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or any(ord(c) < 33 for c in self.base)):
                raise ValueError()
            self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            raise ApiError("slicer_invalid_url") from None
        self.host, self.scheme, self.prefix = parsed.hostname, parsed.scheme, parsed.path.rstrip("/")
        self.key = config.get("slicer_api_key")
        self.configured = isinstance(self.key, str) and bool(re.fullmatch(r"[A-Za-z0-9._~-]{16,256}", self.key))
        self.timeout = config.get("slicer_timeout", 300)
        if (type(self.timeout) not in {int, float} or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 900):
            raise ApiError("slicer_invalid_timeout")
        self.max_input = config.get("slicer_max_input_bytes", 20 * 1024 * 1024)
        self.max_output = config.get("slicer_max_output_bytes", 128 * 1024 * 1024)
        if type(self.max_input) is not int or type(self.max_output) is not int or min(self.max_input, self.max_output) < 1:
            raise ApiError("slicer_invalid_limit")
        self.max_input, self.max_output = min(self.max_input, 20 * 1024 * 1024), min(self.max_output, 128 * 1024 * 1024)

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ApiError("slicer_timeout")
        return remaining

    def _request(self, method, endpoint, deadline, source=None, size=0, temperatures=None):
        if not self.configured:
            raise ApiError("slicer_not_configured")
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        connection = cls(self.host, self.port, timeout=self._remaining(deadline))
        try:
            connection.connect()
            transport = connection.sock
            transport.settimeout(self._remaining(deadline))
            connection.putrequest(method, self.prefix + endpoint)
            connection.putheader("X-Slicer-Key", self.key)
            connection.putheader("Connection", "close")
            if method == "POST":
                connection.putheader("Content-Type", "application/octet-stream")
                connection.putheader("Content-Length", str(size))
                if temperatures is not None:
                    connection.putheader("X-Nozzle-Temperature", format(temperatures["nozzle"], ".12g"))
                    connection.putheader("X-Bed-Temperature", format(temperatures["bed"], ".12g"))
            connection.endheaders()
            if source is not None:
                remaining = size
                while remaining:
                    chunk = source.read(min(65536, remaining))
                    if not chunk:
                        raise ApiError("slicer_input_changed")
                    transport.settimeout(self._remaining(deadline))
                    connection.send(chunk)
                    remaining -= len(chunk)
            transport.settimeout(self._remaining(deadline))
            response = connection.getresponse()
            return connection, response, transport
        except Exception:
            connection.close()
            raise

    def _error(self, response, transport, deadline):
        code = None
        try:
            declared = response.getheader("Content-Length")
            size = min(4096, int(declared)) if declared is not None and declared.isdigit() else 4096
            value = json.loads(self._read_small(response, transport, deadline, size))
            code = value.get("error") if isinstance(value, dict) else None
        except (ValueError, UnicodeError):
            pass
        known = {"busy": "busy", "configuration": "not_ready", "invalid_stl": "invalid_stl",
                 "invalid_model": "invalid_model", "oversize": "oversize", "slice_timeout": "timeout",
                 "slicing_failed": "failed", "invalid_temperature": "invalid_temperature"}
        suffix = known.get(code, "http_" + str(response.status)) if isinstance(code, str) else "http_" + str(response.status)
        raise ApiError("slicer_" + suffix, http_status=response.status)

    def _read_small(self, response, transport, deadline, size):
        chunks = bytearray()
        while len(chunks) < size:
            transport.settimeout(self._remaining(deadline))
            chunk = response.read1(size - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    @staticmethod
    def _size(response, maximum):
        declared = response.getheader("Content-Length")
        try:
            if declared is None or not re.fullmatch(r"[0-9]+", declared):
                raise ValueError()
            size = int(declared)
        except (TypeError, ValueError):
            raise ApiError("slicer_invalid_content_length") from None
        if size > maximum:
            raise ApiError("slicer_output_too_large")
        return size

    @staticmethod
    def _estimate(response, header):
        raw = response.getheader(header)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    def health(self):
        connection, response = None, None
        deadline = time.monotonic() + min(self.timeout, 4)
        try:
            connection, response, transport = self._request("GET", "/health", deadline)
            if response.status != 200:
                self._error(response, transport, deadline)
            size = self._size(response, 4096)
            raw = self._read_small(response, transport, deadline, size)
            if len(raw) != size:
                raise ApiError("slicer_incomplete_response")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ApiError("slicer_invalid_health") from None
            if (not isinstance(value, dict) or type(value.get("ready")) is not bool
                    or type(value.get("busy")) is not bool or value.get("material") != "PETG"
                    or value.get("engine") != "OrcaSlicer"):
                raise ApiError("slicer_invalid_health")
            return value
        except (TimeoutError, socket.timeout):
            raise ApiError("slicer_timeout") from None
        except (OSError, http.client.HTTPException):
            raise ApiError("slicer_network") from None
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

    def slice(self, source_path, destination_path, temperatures=None):
        if temperatures is not None:
            if not isinstance(temperatures, dict) or set(temperatures) != {"nozzle", "bed"}:
                raise ApiError("slicer_invalid_temperature")
            try:
                _temperature("extruder", temperatures["nozzle"])
                _temperature("heater_bed", temperatures["bed"])
                if temperatures["nozzle"] < 170:
                    raise ApiError("invalid_temperature")
            except ApiError:
                raise ApiError("slicer_invalid_temperature") from None
        source_path, target = Path(source_path), Path(destination_path)
        temporary = target.with_name(target.name + ".part-" + uuid.uuid4().hex)
        connection, response = None, None
        deadline = time.monotonic() + self.timeout
        try:
            if not self.configured:
                raise ApiError("slicer_not_configured")
            try:
                descriptor = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            except OSError:
                raise ApiError("slicer_invalid_input") from None
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size < 1:
                    raise ApiError("slicer_invalid_input")
                if info.st_size > self.max_input:
                    raise ApiError("slicer_input_too_large")
                connection, response, transport = self._request("POST", "/slice", deadline, source, info.st_size, temperatures)
            if response.status != 200:
                self._error(response, transport, deadline)
            size = self._size(response, self.max_output)
            if not size:
                raise ApiError("slicer_empty_output")
            metadata = {"path": str(target), "size": size, "material": "PETG",
                        "estimated_time": self._estimate(response, "X-Print-Time-Seconds"),
                        "filament_grams": self._estimate(response, "X-Filament-Grams")}
            try:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except OSError:
                raise ApiError("slicer_output_failed") from None
            with os.fdopen(descriptor, "wb") as output:
                remaining = size
                while remaining:
                    transport.settimeout(self._remaining(deadline))
                    chunk = response.read1(min(65536, remaining))
                    if not chunk:
                        raise ApiError("slicer_incomplete_response")
                    try:
                        output.write(chunk)
                    except OSError:
                        raise ApiError("slicer_output_failed") from None
                    remaining -= len(chunk)
                try:
                    output.flush()
                    os.fsync(output.fileno())
                except OSError:
                    raise ApiError("slicer_output_failed") from None
            self._remaining(deadline)
            try:
                os.replace(temporary, target)
            except OSError:
                raise ApiError("slicer_output_failed") from None
            return metadata
        except (TimeoutError, socket.timeout):
            raise ApiError("slicer_timeout") from None
        except (OSError, http.client.HTTPException):
            raise ApiError("slicer_network") from None
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
