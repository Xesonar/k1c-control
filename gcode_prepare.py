"""Prepare a byte-exact M25 gate after the stock K1C START_PRINT.

The input is never executed or rewritten. Extra lines reset object metadata,
provide verified bounds when available and save/pause before model extrusion.
Reinspect Moonraker's uploaded copy: its object preprocessor can insert bytes.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from adapters import ApiError

MAX_SIZE = 128 * 1024 * 1024
MAX_LINE = 65536
TOKEN = re.compile(r"[A-Za-z0-9_]{8,64}\Z")
NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
PREFIX_SETUP = {
    "EXCLUDE_OBJECT_DEFINE", "SET_PRINT_STATS_INFO", "SET_VELOCITY_LIMIT",
    "SET_PRESSURE_ADVANCE", "SET_GCODE_OFFSET", "SET_FAN_SPEED",
    "SET_HEATER_TEMPERATURE", "SET_RETRACTION",
    "G0", "G1", "G2", "G3", "G21", "G28", "G90", "G91", "G92",
    "M73", "M82", "M83", "M104", "M109", "M140", "M190", "M106", "M107",
    "M117", "M201", "M203", "M204", "M205", "M220", "M221", "M400",
}


def _token(token):
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ApiError("gcode_invalid_gate_token")
    return token


def _lines(stream):
    offset = 0
    while True:
        raw = stream.readline(MAX_LINE + 1)
        if not raw:
            return
        if len(raw) > MAX_LINE or b"\x00" in raw:
            raise ApiError("gcode_unsupported_format")
        yield offset, raw
        offset += len(raw)
        if offset > MAX_SIZE:
            raise ApiError("gcode_too_large")


def _code(raw):
    try:
        text = raw.decode("utf-8").split(";", 1)[0].strip()
    except UnicodeError:
        raise ApiError("gcode_unsupported_format") from None
    if not text:
        return "", ""
    if re.match(r"N[0-9]+(?:\s|[A-Za-z])", text, re.I):
        raise ApiError("gcode_unsupported_numbered_file")
    # Klipper accepts packed G-code such as G1X110Y110E10 and G1 X110Y110E10.
    # The command is the leading letter/number, not the first whitespace token.
    packed = re.match(r"^([GMT])\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
    if packed:
        number = packed[2] if "." in packed[2] else str(int(packed[2]))
        return packed[1].upper() + number, text
    return text.split()[0].upper(), text


def _temperatures(text):
    values = {}
    for item in text.split()[1:]:
        if "=" not in item:
            raise ApiError("gcode_unsupported_start")
        key, value = item.split("=", 1)
        key = key.upper()
        if key not in {"BED_TEMP", "EXTRUDER_TEMP"}:
            continue
        if key in values or not re.fullmatch(NUMBER, value):
            raise ApiError("gcode_invalid_start_temperature")
        values[key] = float(value)
    if set(values) != {"BED_TEMP", "EXTRUDER_TEMP"}:
        raise ApiError("gcode_missing_start_temperature")
    nozzle, bed = values["EXTRUDER_TEMP"], values["BED_TEMP"]
    if not math.isfinite(nozzle) or not math.isfinite(bed) or not 170 <= nozzle <= 300 or not 0 <= bed <= 100:
        raise ApiError("gcode_invalid_start_temperature")
    return bed, nozzle


def _polygon(text):
    match = re.search(r"\bPOLYGON\s*=\s*", text, re.I)
    if not match:
        return False
    try:
        polygon, _ = json.JSONDecoder().raw_decode(text[match.end():])
        if not isinstance(polygon, list) or not 3 <= len(polygon) <= 4096:
            raise ValueError()
        for point in polygon:
            if (not isinstance(point, list) or len(point) != 2
                    or any(type(x) not in {int, float} or not math.isfinite(x) or not 0 <= x <= 220 for x in point)):
                raise ValueError()
        if len({point[0] for point in polygon}) < 2 or len({point[1] for point in polygon}) < 2:
            raise ValueError()
    except (ValueError, TypeError):
        raise ApiError("gcode_invalid_object_bounds") from None
    return {"min": [min(p[0] for p in polygon), min(p[1] for p in polygon)],
            "max": [max(p[0] for p in polygon), max(p[1] for p in polygon)]}


def _scan(stream, token, inspect=False):
    digest, starts, objects, header_bounds = hashlib.sha256(), [], [], {}
    gate, save_seen, previous, reset, early_extrusion = None, 0, "", None, False
    state_name = "K1C_GATE_" + token
    marker = "K1C_BOT_GATE_" + token
    save = "SAVE_GCODE_STATE NAME=" + state_name
    for offset, raw in _lines(stream):
        digest.update(raw)
        command, text = _code(raw)
        decoded = raw.decode("utf-8")
        if "K1C_BOT_GATE_" in decoded or "K1C_GATE_" in decoded:
            if not inspect:
                raise ApiError("gcode_gate_collision")
            if text == save:
                save_seen += 1
                if not starts or previous.split()[0].upper() != "START_PRINT":
                    raise ApiError("gcode_invalid_gate_order")
            elif text == "M25" and decoded.strip() == "M25 ; " + marker:
                if gate is not None or previous != save or save_seen != 1:
                    raise ApiError("gcode_gate_collision")
                gate = {"start": offset, "end": offset + len(raw), "token": token, "state_name": state_name}
            else:
                raise ApiError("gcode_gate_collision")
        if command == "START_PRINT":
            if early_extrusion:
                raise ApiError("gcode_model_before_start")
            bed, nozzle = _temperatures(text)
            starts.append({"offset": offset, "end": offset + len(raw), "raw": raw, "bed": bed, "nozzle": nozzle})
            if len(starts) > 1:
                raise ApiError("gcode_repeated_start")
        elif command in {"SDCARD_PRINT_FILE", "M23", "M24", "M26", "RESTART", "FIRMWARE_RESTART", "SAVE_CONFIG"}:
            raise ApiError("gcode_unsupported_command")
        elif starts and (command in {"BED_MESH_CLEAR", "BED_MESH_CALIBRATE", "G29"}
                         or command == "BED_MESH_PROFILE" and re.search(r"\bLOAD\s*=", text, re.I)):
            raise ApiError("gcode_mesh_changed_after_start")
        elif not starts and command in {"G0", "G1", "G2", "G3"} and "E" in text.upper():
            early_extrusion = True
        elif not starts and command and command not in PREFIX_SETUP:
            # Unknown printer macros can hide extrusion or load another print.
            # Only known setup operations may precede the mandatory native start.
            raise ApiError("gcode_unsupported_prefix")
        if not starts:
            match = re.fullmatch(r"\s*;\s*(MINX|MINY|MAXX|MAXY)\s*[:=]\s*(" + NUMBER + r")\s*", decoded, re.I)
            if match:
                header_bounds[match[1].upper()] = float(match[2])
            if command == "EXCLUDE_OBJECT_DEFINE":
                if re.search(r"\bRESET\s*=\s*1(?:\s|$)", text, re.I):
                    reset, objects = offset, []
                else:
                    polygon = _polygon(text)
                    if polygon:
                        objects.append({"offset": offset, "bbox": polygon})
        if command:
            previous = text
    if len(starts) != 1:
        raise ApiError("gcode_missing_start")
    start = starts[0]
    if not objects:
        keys = {"MINX", "MINY", "MAXX", "MAXY"}
        if (set(header_bounds) != keys or not all(math.isfinite(x) for x in header_bounds.values())
                or not 0 <= header_bounds["MINX"] < header_bounds["MAXX"] <= 220
                or not 0 <= header_bounds["MINY"] < header_bounds["MAXY"] <= 220):
            raise ApiError("gcode_missing_object_bounds")
        bbox = {"min": [header_bounds["MINX"], header_bounds["MINY"]],
                "max": [header_bounds["MAXX"], header_bounds["MAXY"]]}
    else:
        bbox = {"min": [min(obj["bbox"]["min"][axis] for obj in objects) for axis in range(2)],
                "max": [max(obj["bbox"]["max"][axis] for obj in objects) for axis in range(2)]}
    if inspect and (gate is None or save_seen != 1):
        raise ApiError("gcode_missing_gate")
    return {"start": start, "objects": objects, "bounds": header_bounds, "reset": reset,
            "gate": gate, "sha256": digest.hexdigest(), "bbox": bbox}


def _open(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        stream = os.fdopen(descriptor, "rb")
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SIZE:
            stream.close()
            raise ApiError("gcode_invalid_file")
        return stream
    except OSError:
        raise ApiError("gcode_invalid_file") from None


def inspect_gate(path, token):
    token = _token(token)
    with _open(path) as stream:
        result = _scan(stream, token, inspect=True)
    gate = result["gate"]
    gate.update(bed=result["start"]["bed"], nozzle=result["start"]["nozzle"], sha256=result["sha256"], bbox=result["bbox"])
    return gate


def prepare_gcode(source_path, destination_path, token):
    token = _token(token)
    target = Path(destination_path)
    if Path(source_path).resolve() == target.resolve():
        raise ApiError("gcode_same_path")
    temporary = target.with_name(target.name + ".part-" + uuid.uuid4().hex)
    try:
        with _open(source_path) as source:
            result = _scan(source, token)
            start = result["start"]
            newline = b"\r\n" if start["raw"].endswith(b"\r\n") else b"\n"
            inserts = {}
            if result["objects"]:
                if result["reset"] is None:
                    inserts[result["objects"][0]["offset"]] = b"EXCLUDE_OBJECT_DEFINE RESET=1" + newline
            else:
                b = result["bounds"]
                polygon = [[b["MINX"], b["MINY"]], [b["MAXX"], b["MINY"]], [b["MAXX"], b["MAXY"]],
                           [b["MINX"], b["MAXY"]], [b["MINX"], b["MINY"]]]
                define = (f"EXCLUDE_OBJECT_DEFINE NAME=K1C_MODEL_{token} CENTER={(b['MINX']+b['MAXX'])/2:g},{(b['MINY']+b['MAXY'])/2:g} POLYGON="
                          + json.dumps(polygon, separators=(",", ":"))).encode()
                inserts[start["offset"]] = b"EXCLUDE_OBJECT_DEFINE RESET=1" + newline + define + newline
            state = ("SAVE_GCODE_STATE NAME=K1C_GATE_" + token).encode()
            pause = ("M25 ; K1C_BOT_GATE_" + token).encode()
            suffix = (b"" if start["raw"].endswith(b"\n") else newline) + state + newline + pause + newline
            source.seek(0)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                copied = hashlib.sha256()
                for offset, raw in _lines(source):
                    copied.update(raw)
                    if offset in inserts:
                        output.write(inserts[offset])
                    output.write(raw)
                    if offset == start["offset"]:
                        output.write(suffix)
                    if output.tell() > MAX_SIZE:
                        raise ApiError("gcode_too_large")
                if copied.hexdigest() != result["sha256"]:
                    raise ApiError("gcode_file_changed")
                output.flush()
                os.fsync(output.fileno())
        gate = inspect_gate(temporary, token)
        os.replace(temporary, target)
        gate["path"] = str(target)
        return gate
    except OSError:
        raise ApiError("gcode_prepare_failed") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
