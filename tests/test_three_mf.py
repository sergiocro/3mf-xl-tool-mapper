import base64
import hashlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

import app as app_module
import uvicorn
from paint_codec import decode_paint_color, encode_paint_color, paint_states, remap_paint_color
from prusa_native import CORE, MODEL_CONFIG, ROOT_MODEL, SLIC3RPE
from three_mf import MODEL_SETTINGS, PROJECT_SETTINGS, ThreeMFError, export_archive, inspect_archive, sha256_path


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _metadata(parent):
    return {
        child.get("key"): child.get("value")
        for child in parent
        if _local(child.tag) == "metadata" and child.get("key")
    }


def _config_tools(data: bytes):
    root = ET.fromstring(data)
    tools = []
    volumes = []
    for obj in root:
        if _local(obj.tag) != "object":
            continue
        meta = _metadata(obj)
        tools.append(int(meta["extruder"]) if meta.get("extruder") else None)
        volumes.extend(child for child in obj if _local(child.tag) == "volume")
    return tools, volumes


def _mmu_values(data: bytes):
    root = ET.fromstring(data)
    return [
        triangle.get(f"{{{SLIC3RPE}}}mmu_segmentation")
        for triangle in root.iter()
        if _local(triangle.tag) == "triangle" and triangle.get(f"{{{SLIC3RPE}}}mmu_segmentation")
    ]


def _stream_native_model(archive: zipfile.ZipFile):
    states = Counter()
    transforms = []
    painted = vertices = triangles = objects = 0
    with archive.open(ROOT_MODEL) as stream:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            tag = _local(element.tag)
            if event == "start" and tag == "object":
                objects += 1
            elif event == "end" and tag == "vertex":
                vertices += 1
            elif event == "end" and tag == "triangle":
                if "paint_color" in element.attrib:
                    raise AssertionError("paint_color found in Prusa-native model")
                value = element.get(f"{{{SLIC3RPE}}}mmu_segmentation")
                if value:
                    states.update(paint_states(decode_paint_color(value)))
                    painted += 1
                triangles += 1
            elif event == "end" and tag == "item":
                transforms.append(element.get("transform", ""))
            if event == "end":
                element.clear()
    return {
        "states": dict(sorted(states.items())),
        "transforms": transforms,
        "painted": painted,
        "vertices": vertices,
        "triangles": triangles,
        "objects": objects,
    }


def fixture(path: Path, component_transform: str = "1 0 0 0 1 0 0 0 1 0 0 0"):
    root_model = f'''<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="{CORE}" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">
 <resources>
  <object id="2" type="model"><components><component objectid="1" p:path="/3D/Objects/object_1.model" transform="{component_transform}"/></components></object>
  <object id="4" type="model"><components><component objectid="3" p:path="/3D/Objects/object_2.model"/></components></object>
  <object id="6" type="model"><components><component objectid="5" p:path="/3D/Objects/object_3.model"/></components></object>
 </resources>
 <build>
  <item objectid="2" transform="1 0 0 0 1 0 0 0 1 100 10 2" printable="1"/>
  <item objectid="4" transform="0 -1 0 1 0 0 0 0 1 50 60 3" printable="1"/>
  <item objectid="6" transform="1 0 0 0 1 0 0 0 1 20 30 4" printable="1"/>
 </build>
</model>'''.encode()
    settings = b'''<?xml version="1.0"?><config>
 <object id="2"><metadata key="name" value="Painted.stl"/><metadata key="extruder" value="3"/><part id="1"><metadata key="name" value="Painted part"/></part></object>
 <object id="4"><metadata key="name" value="Top.stl"/><metadata key="extruder" value="4"/><part id="3"><metadata key="name" value="Top part"/></part></object>
 <object id="6"><metadata key="name" value="Bottom.stl"/><metadata key="extruder" value="3"/><part id="5"><metadata key="name" value="Bottom part"/></part></object>
</config>'''
    models = {
        "3D/Objects/object_1.model": f'''<model xmlns="{CORE}"><resources><object id="1" type="model" name="Painted part"><mesh><vertices>
 <vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/>
</vertices><triangles>
 <triangle v1="0" v2="1" v3="2" paint_color="4"/>
 <triangle v1="0" v2="1" v3="2" paint_color="8"/>
 <triangle v1="0" v2="1" v3="2" paint_color="0C"/>
 <triangle v1="0" v2="1" v3="2" paint_color="1C"/>
 <triangle v1="0" v2="1" v3="2" paint_color="1C1C02"/>
</triangles></mesh></object></resources></model>'''.encode(),
        "3D/Objects/object_2.model": f'''<model xmlns="{CORE}"><resources><object id="3" type="model" name="Top part"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/><vertex x="0" y="2" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>'''.encode(),
        "3D/Objects/object_3.model": f'''<model xmlns="{CORE}"><resources><object id="5" type="model" name="Bottom part"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="3" y="0" z="0"/><vertex x="0" y="3" z="0"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources></model>'''.encode(),
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "content-types")
        archive.writestr("_rels/.rels", "rels")
        archive.writestr(ROOT_MODEL, root_model)
        archive.writestr(MODEL_SETTINGS, settings)
        archive.writestr(PROJECT_SETTINGS, json.dumps({"filament_colour": ["#FFFFFF", "#000000", "#FF80C0", "#FFFF00"]}))
        for name, data in models.items():
            archive.writestr(name, data)
        archive.writestr("Metadata/thumbnail.png", PNG_1X1)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = Path(self.tmp.name) / "source.3mf"
        fixture(self.source)
        self.original_hash = sha256_path(self.source)

    def tearDown(self):
        self.tmp.cleanup()

    def export(self, mapping, confirm=False, name="out.3mf"):
        out = Path(self.tmp.name) / name
        return out, export_archive(self.source, out, mapping, confirm)

    def test_identity_mapping_is_semantically_identical(self):
        out, report = self.export({1: 1, 2: 2, 3: 3, 4: 4})
        self.assertEqual(report["format"], "prusa-native")
        self.assertEqual(report["paint_states_before"], report["paint_states_after"])
        self.assertEqual(report["paint_values_changed"], 0)
        with zipfile.ZipFile(out) as archive:
            self.assertEqual(_mmu_values(archive.read(ROOT_MODEL)), ["4", "8", "0C", "1C", "1C1C02"])
        self.assertEqual(sha256_path(self.source), self.original_hash)

    def test_target_mapping_becomes_native_prusa_paint(self):
        mapping = {1: 2, 2: 1, 3: 4, 4: 5}
        out, report = self.export(mapping)
        self.assertEqual(report["tools"], [4, 5, 4])
        self.assertEqual(report["paint_states_before"], {0: 1, 1: 1, 2: 1, 3: 1, 4: 3})
        self.assertEqual(report["paint_states_after"], {0: 1, 1: 1, 2: 1, 4: 1, 5: 3})
        with zipfile.ZipFile(out) as archive:
            model = archive.read(ROOT_MODEL)
            self.assertNotIn(b"paint_color", model)
            self.assertIn(b"slic3rpe:mmu_segmentation", model)
            self.assertEqual(_mmu_values(model), ["8", "4", "1C", "2C", "2C2C02"])
            tools, volumes = _config_tools(archive.read(MODEL_CONFIG))
            self.assertEqual(tools, [4, 5, 4])
            self.assertEqual(len(volumes), 3)
            self.assertTrue(all(_metadata(volume)["volume_type"] == "ModelPart" for volume in volumes))

    def test_output_package_replaces_bambu_project_structure(self):
        out, report = self.export({1: 2, 2: 1, 3: 4, 4: 5})
        with zipfile.ZipFile(out) as archive:
            names = archive.namelist()
            self.assertIn(ROOT_MODEL, names)
            self.assertIn(MODEL_CONFIG, names)
            self.assertNotIn(MODEL_SETTINGS, names)
            self.assertNotIn(PROJECT_SETTINGS, names)
            self.assertFalse(any(name.startswith("3D/Objects/") for name in names))
            root = ET.fromstring(archive.read(ROOT_MODEL))
            self.assertEqual(root.find(f"{{{CORE}}}metadata[@name='slic3rpe:Version3mf']").text, "1")
            self.assertEqual(root.find(f"{{{CORE}}}metadata[@name='slic3rpe:MmPaintingVersion']").text, "1")
        self.assertTrue(report["prusa_parse_ok"])

    def test_component_transform_is_baked_and_build_transforms_are_preserved(self):
        transformed = Path(self.tmp.name) / "transformed.3mf"
        fixture(transformed, "1 0 0 0 1 0 0 0 1 10 20 30")
        out = Path(self.tmp.name) / "transformed-out.3mf"
        report = export_archive(transformed, out, {1: 2, 2: 1, 3: 4, 4: 5})
        self.assertEqual(report["component_transforms_baked"], 1)
        self.assertEqual(report["expected_vertex_sha256"], report["output_vertex_sha256"])
        self.assertEqual(report["expected_triangle_sha256"], report["output_triangle_sha256"])
        self.assertTrue(report["build_transforms_preserved"])
        with zipfile.ZipFile(out) as archive:
            root = ET.fromstring(archive.read(ROOT_MODEL))
            first_vertex = next(element for element in root.iter() if _local(element.tag) == "vertex")
            self.assertEqual(tuple(float(first_vertex.get(axis)) for axis in ("x", "y", "z")), (10.0, 20.0, 30.0))
            transforms = [element.get("transform") for element in root.iter() if _local(element.tag) == "item"]
            self.assertEqual(transforms, [
                "1 0 0 0 1 0 0 0 1 100 10 2",
                "0 -1 0 1 0 0 0 0 1 50 60 3",
                "1 0 0 0 1 0 0 0 1 20 30 4",
            ])

    def test_whole_leaf_and_complex_codec_mapping(self):
        mapping = {1: 2, 2: 1, 3: 4, 4: 5}
        values = ("4", "8", "0C", "1C", "1C1C02")
        actual = [encode_paint_color(remap_paint_color(decode_paint_color(value), mapping)) for value in values]
        self.assertEqual(actual, ["8", "4", "1C", "2C", "2C2C02"])
        self.assertEqual(list(paint_states(decode_paint_color(actual[-1]))), [0, 5, 5])

    def test_source_project_and_thumbnail_remain_unchanged(self):
        out, _ = self.export({1: 2, 2: 1, 3: 4, 4: 5})
        self.assertEqual(sha256_path(self.source), self.original_hash)
        with zipfile.ZipFile(self.source) as before, zipfile.ZipFile(out) as after:
            self.assertEqual(before.read("Metadata/thumbnail.png"), after.read("Metadata/thumbnail.png"))
            self.assertNotIn(PROJECT_SETTINGS, after.namelist())

    def test_all_filament_colour_slots_stay_available_to_ui(self):
        info = inspect_archive(self.source)
        self.assertIsInstance(info["display_tools"], list)
        self.assertEqual([tool["tool"] for tool in info["display_tools"]], [1, 2, 3, 4])
        self.assertEqual([tool["color"] for tool in info["display_tools"]], ["#FFFFFF", "#000000", "#FF80C0", "#FFFF00"])
        self.assertNotIn("used", info["display_tools"][0])

    def test_display_tools_is_always_a_list_without_filament_metadata(self):
        bare = Path(self.tmp.name) / "bare.3mf"
        with zipfile.ZipFile(bare, "w") as archive:
            archive.writestr(MODEL_SETTINGS, b'<config><object id="1"><metadata key="extruder" value="2"/></object></config>')
        self.assertEqual(inspect_archive(bare)["display_tools"], [])

    def test_conflicting_mapping_requires_confirmation(self):
        with self.assertRaises(ThreeMFError):
            self.export({1: 1, 2: 2, 3: 1, 4: 1})
        _, report = self.export({1: 1, 2: 2, 3: 1, 4: 1}, True)
        self.assertEqual(report["tools"], [1, 1, 1])

    def test_missing_painted_mapping_stops_export(self):
        with self.assertRaises(ThreeMFError):
            self.export({3: 4, 4: 5})

    def test_invalid_3mf(self):
        bad = Path(self.tmp.name) / "bad.3mf"
        bad.write_bytes(b"not zip")
        with self.assertRaises(ThreeMFError):
            inspect_archive(bad)

    def test_original_cannot_be_overwritten(self):
        with self.assertRaises(ThreeMFError):
            export_archive(self.source, self.source, {1: 2, 2: 1, 3: 4, 4: 5})

    def test_same_upload_exports_use_unique_paths(self):
        old_work, old_exports = app_module.WORK, app_module.EXPORTS
        app_module.WORK = Path(self.tmp.name) / "work"
        app_module.EXPORTS = Path(self.tmp.name) / "exports"
        app_module.WORK.mkdir()
        app_module.EXPORTS.mkdir()
        token = "same-upload"
        (app_module.WORK / f"{token}.3mf").write_bytes(self.source.read_bytes())
        try:
            first = app_module.export(token, "output.3mf", '{"1":2,"2":1,"3":4,"4":5}', False)
            second = app_module.export(token, "output.3mf", '{"1":1,"2":2,"3":3,"4":4}', False)
            self.assertNotEqual(Path(first.path), Path(second.path))
            self.assertNotEqual(first.headers["x-export-id"], second.headers["x-export-id"])
        finally:
            app_module.WORK, app_module.EXPORTS = old_work, old_exports

    def test_real_bee_http_artifact_identity_and_native_mapping(self):
        configured = os.environ.get("BEE_3MF")
        bee = Path(configured) if configured else Path(__file__).resolve().parents[1] / ".work" / "rah5wg6c.3mf"
        if not bee.is_file():
            self.skipTest("Real Bee reference file is not available.")

        old_work, old_exports = app_module.WORK, app_module.EXPORTS
        app_module.WORK = Path(self.tmp.name) / "http-work"
        app_module.EXPORTS = Path(self.tmp.name) / "http-exports"
        app_module.WORK.mkdir()
        app_module.EXPORTS.mkdir()
        server_socket = socket.socket()
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen()
        port = server_socket.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app_module.app, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [server_socket]}, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                time.sleep(0.01)
            self.assertTrue(server.started)
            boundary = f"----ToolMapper{uuid.uuid4().hex}"
            upload = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{bee.name}\"\r\n"
                "Content-Type: model/3mf\r\n\r\n"
            ).encode() + bee.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/inspect",
                data=upload,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                token = json.loads(response.read())["token"]

            body = urllib.parse.urlencode({
                "token": token,
                "filename": "Bee Clicker_Prusa_XL.3mf",
                "mapping": '{"1":2,"2":1,"3":4,"4":5}',
                "confirm_conflict": "false",
            }).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/export",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=240) as response:
                headers = response.headers
                response_bytes = response.read()

            export_id = headers["X-Export-ID"]
            uuid.UUID(export_id)
            self.assertEqual(headers["X-Export-Mapping"], "1-2,2-1,3-4,4-5")
            response_sha = hashlib.sha256(response_bytes).hexdigest()
            self.assertEqual(response_sha, headers["X-Export-SHA256"])
            output = app_module.EXPORTS / export_id / "Bee Clicker_Prusa_XL.3mf"
            self.assertEqual(output.read_bytes(), response_bytes)

            with zipfile.ZipFile(io.BytesIO(response_bytes)) as generated, zipfile.ZipFile(bee) as source:
                self.assertNotIn(MODEL_SETTINGS, generated.namelist())
                self.assertNotIn(PROJECT_SETTINGS, generated.namelist())
                self.assertFalse(any(name.startswith("3D/Objects/") for name in generated.namelist()))
                tools, volumes = _config_tools(generated.read(MODEL_CONFIG))
                self.assertEqual(tools, [5, 5, 1])
                self.assertEqual(len(volumes), 3)
                model_stats = _stream_native_model(generated)
                self.assertEqual(model_stats["states"], {0: 39299, 1: 189121, 2: 248878, 4: 229088, 5: 307512})
                self.assertEqual(model_stats["objects"], 3)
                self.assertEqual(model_stats["vertices"], 604431)
                self.assertEqual(model_stats["triangles"], 1208686)
                self.assertEqual(model_stats["painted"], 925962)
                source_root = ET.fromstring(source.read(ROOT_MODEL))
                source_transforms = [element.get("transform", "") for element in source_root.iter() if _local(element.tag) == "item"]
                self.assertEqual(model_stats["transforms"], source_transforms)
                self.assertEqual(generated.read("Metadata/plate_1.png"), source.read("Metadata/plate_1.png"))
            report = json.loads(headers["X-Validation-Report"])
            self.assertTrue(report["prusa_parse_ok"])
            self.assertEqual(report["objects"], 3)
            self.assertEqual(report["vertices"], 604431)
            self.assertEqual(report["triangles"], 1208686)
            self.assertEqual(report["expected_vertex_sha256"], report["output_vertex_sha256"])
            self.assertEqual(report["expected_triangle_sha256"], report["output_triangle_sha256"])
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            server_socket.close()
            app_module.WORK, app_module.EXPORTS = old_work, old_exports


if __name__ == "__main__":
    unittest.main()
