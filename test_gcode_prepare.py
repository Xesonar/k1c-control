import tempfile
import unittest
from pathlib import Path

from adapters import ApiError
from gcode_prepare import inspect_gate, prepare_gcode

TOKEN = "abcdef0123456789"
OBJECT = b"EXCLUDE_OBJECT_DEFINE NAME=model CENTER=110,110 POLYGON=[[100,100],[120,100],[120,120],[100,120],[100,100]]\n"
START = b"START_PRINT EXTRUDER_TEMP=240 BED_TEMP=70\n"
MODEL = b"G90\nM83\nG1 X110 Y110 E0.1\nM104 S235\nM140 S65\n"


class GcodePrepareTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.source = Path(self.folder.name) / "source.gcode"
        self.target = Path(self.folder.name) / "prepared.gcode"

    def tearDown(self):
        self.folder.cleanup()

    def prepare(self, content):
        self.source.write_bytes(content)
        return prepare_gcode(self.source, self.target, TOKEN)

    def test_gate_is_after_start_before_model_and_preserves_original_temperatures(self):
        original = b"; comment START_PRINT\n" + OBJECT + b"M140 S0\nM104 S0\n" + START + MODEL
        gate = self.prepare(original)
        output = self.target.read_bytes()
        marker = b"M25 ; K1C_BOT_GATE_" + TOKEN.encode() + b"\n"
        self.assertEqual(output[gate["start"]:gate["end"]], marker)
        self.assertLess(output.index(START), gate["start"])
        self.assertLess(gate["end"], output.index(b"G1 X110"))
        self.assertIn(b"SAVE_GCODE_STATE NAME=K1C_GATE_" + TOKEN.encode() + b"\n" + marker, output)
        self.assertNotIn(b"PAUSE", output)
        self.assertIn(MODEL, output)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual((gate["bed"], gate["nozzle"]), (70, 240))
        self.assertEqual(gate["state_name"], "K1C_GATE_" + TOKEN)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_reinspection_uses_byte_offsets_after_moonraker_inserts_utf8_lines(self):
        gate = self.prepare(OBJECT + START + MODEL)
        inserted = "; метаданные Moonraker\n".encode()
        self.target.write_bytes(inserted + self.target.read_bytes())
        inspected = inspect_gate(self.target, TOKEN)
        self.assertEqual(inspected["end"], gate["end"] + len(inserted))
        self.assertEqual(inspected["start"], gate["start"] + len(inserted))
        self.assertEqual(self.target.read_bytes()[inspected["start"]:inspected["end"]], b"M25 ; K1C_BOT_GATE_" + TOKEN.encode() + b"\n")

    def test_crlf_and_comment_commands_are_preserved(self):
        original = (b"; START_PRINT BED_TEMP=1 EXTRUDER_TEMP=2\n" + OBJECT + START + MODEL).replace(b"\n", b"\r\n")
        gate = self.prepare(original)
        output = self.target.read_bytes()
        self.assertTrue(output[gate["start"]:gate["end"]].endswith(b"\r\n"))
        self.assertIn(MODEL.replace(b"\n", b"\r\n"), output)

    def test_missing_object_definitions_use_only_valid_header_bounds(self):
        gate = self.prepare(b";MINX:100\n;MINY:90\n;MAXX:120\n;MAXY:130\n" + START + MODEL)
        content = self.target.read_bytes()
        self.assertLess(content.index(b"EXCLUDE_OBJECT_DEFINE NAME=K1C_MODEL_"), content.index(START))
        self.assertEqual(inspect_gate(self.target, TOKEN)["end"], gate["end"])
        self.prepare(b"; MINX = 100.00\n; MINY = 90.00\n; MAXX = 120.00\n; MAXY = 130.00\n" + START + MODEL)
        self.assertEqual(inspect_gate(self.target, TOKEN)["bbox"], {"min": [100.0, 90.0], "max": [120.0, 130.0]})

    def test_existing_reset_stays_before_objects_without_an_extra_reset(self):
        self.prepare(b"EXCLUDE_OBJECT_DEFINE RESET=1\n" + OBJECT + START + MODEL)
        self.assertEqual(self.target.read_bytes().count(b"EXCLUDE_OBJECT_DEFINE RESET=1"), 1)

    def test_unsupported_mesh_changes_repeated_macro_and_model_before_start_are_rejected(self):
        invalid = [OBJECT + MODEL + START, OBJECT + START + START + MODEL,
                   OBJECT + START + b"BED_MESH_CLEAR\n" + MODEL,
                   OBJECT + START + b"BED_MESH_PROFILE LOAD=default\n" + MODEL,
                   OBJECT + START + b"BED_MESH_CALIBRATE\n" + MODEL,
                   OBJECT + START + b"SDCARD_PRINT_FILE FILENAME=other.gcode\n",
                   b"G1 X10 E1\n" + OBJECT + START + MODEL]
        for content in invalid:
            with self.subTest(content=content[:60]), self.assertRaises(ApiError):
                self.prepare(content)

    def test_packed_extrusion_cannot_bypass_the_gate_before_start(self):
        for move in (b"G1X110Y110E10", b"G1 X110Y110E10", b"G1X110 Y110 E10",
                     b"g01x110y110e10", b"G 1 X110Y110E10", b"G2X110Y110I1J1E1"):
            with self.subTest(move=move), self.assertRaises(ApiError) as caught:
                self.prepare(OBJECT + move + b"\n" + START + MODEL)
            self.assertEqual(caught.exception.code, "gcode_model_before_start")
        with self.assertRaises(ApiError) as caught:
            self.prepare(OBJECT + b"N1G1X110Y110E10\n" + START + MODEL)
        self.assertEqual(caught.exception.code, "gcode_unsupported_numbered_file")

    def test_unknown_prefix_macros_cannot_extrude_before_the_native_start(self):
        for command in (b"PRINT_START", b"DRAW_LINE", b"CUSTOM_PURGE X=10", b"M701"):
            with self.subTest(command=command), self.assertRaises(ApiError) as caught:
                self.prepare(OBJECT + command + b"\n" + START + MODEL)
            self.assertEqual(caught.exception.code, "gcode_unsupported_prefix")
        setup = b"M73 P0 R10\nM106 S0\nSET_PRINT_STATS_INFO TOTAL_LAYER=10\nM140 S0\nM104 S0\nG90\nG21\nM83\nG92 E0\n"
        self.prepare(OBJECT + setup + START + MODEL)
        self.assertIn(setup, self.target.read_bytes())

    def test_missing_start_bounds_or_invalid_target_have_friendly_codes(self):
        cases = [(OBJECT + MODEL, "gcode_missing_start"),
                 (START + MODEL, "gcode_missing_object_bounds"),
                 (OBJECT + b"START_PRINT EXTRUDER_TEMP=400 BED_TEMP=70\n" + MODEL, "gcode_invalid_start_temperature"),
                 (OBJECT + b"START_PRINT EXTRUDER_TEMP=nan BED_TEMP=70\n" + MODEL, "gcode_invalid_start_temperature"),
                 (OBJECT + b"START_PRINT EXTRUDER_TEMP=240\n" + MODEL, "gcode_missing_start_temperature")]
        for content, code in cases:
            with self.subTest(code=code), self.assertRaises(ApiError) as caught:
                self.prepare(content)
            self.assertEqual(caught.exception.code, code)

    def test_existing_or_colliding_gate_cannot_be_nested_or_forged(self):
        self.prepare(OBJECT + START + MODEL)
        prepared = self.target.read_bytes()
        self.source.write_bytes(prepared)
        with self.assertRaises(ApiError) as caught:
            prepare_gcode(self.source, self.target, TOKEN)
        self.assertEqual(caught.exception.code, "gcode_gate_collision")
        self.target.write_bytes(prepared + b"M25 ; K1C_BOT_GATE_" + TOKEN.encode() + b"\n")
        with self.assertRaises(ApiError):
            inspect_gate(self.target, TOKEN)
        self.target.write_bytes(prepared.replace(b"M25 ; K1C_BOT_GATE_", b"PAUSE ; K1C_BOT_GATE_"))
        with self.assertRaises(ApiError):
            inspect_gate(self.target, TOKEN)

    def test_bad_token_and_bad_object_bounds_are_rejected_without_overwriting_output(self):
        self.source.write_bytes(OBJECT + START + MODEL)
        self.target.write_bytes(b"previous")
        for token in ("short", "token\nM24", "abcdefgh;M24"):
            with self.subTest(token=token), self.assertRaises(ApiError):
                prepare_gcode(self.source, self.target, token)
        self.assertEqual(self.target.read_bytes(), b"previous")
        with self.assertRaises(ApiError) as caught:
            self.prepare(OBJECT.replace(b"120,120", b"300,120") + START + MODEL)
        self.assertEqual(caught.exception.code, "gcode_invalid_object_bounds")
        self.assertEqual(self.target.read_bytes(), b"previous")
        self.assertFalse(any('.part-' in path.name for path in Path(self.folder.name).iterdir()))


if __name__ == "__main__":
    unittest.main()
