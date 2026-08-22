from __future__ import annotations

import hashlib
import posixpath
import struct
import zipfile
from collections import Counter, deque
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

from paint_codec import PaintCodecError, decode_paint_color, encode_paint_color, paint_states, remap_paint_color


CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PRODUCTION = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
SLIC3RPE = "http://schemas.slic3r.org/3mf/2017/06"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
ROOT_MODEL = "3D/3dmodel.model"
MODEL_SETTINGS = "Metadata/model_settings.config"
MODEL_CONFIG = "Metadata/Slic3r_PE_model.config"

CONTENT_TYPES_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
</Types>
'''

RELS_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{RELS}">
 <Relationship Target="/{ROOT_MODEL}" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
 <Relationship Target="/Metadata/thumbnail.png" Id="rel-2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>
</Relationships>
'''.encode()

IDENTITY_12 = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


class PrusaNativeError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_attr(value: object) -> str:
    return escape(str(value), quote=True)


def _metadata(parent: ET.Element) -> dict[str, str]:
    return {
        child.get("key", ""): child.get("value", "")
        for child in parent
        if _local(child.tag) == "metadata" and child.get("key")
    }


def _parse_xml(data: bytes, label: str) -> ET.Element:
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise PrusaNativeError(f"{label} contains a forbidden XML declaration.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise PrusaNativeError(f"Unreadable XML in {label}: {exc}") from exc


def _assert_safe_member(archive: zipfile.ZipFile, name: str) -> None:
    tail = b""
    with archive.open(name) as stream:
        while chunk := stream.read(1024 * 1024):
            sample = (tail + chunk).upper()
            if b"<!DOCTYPE" in sample or b"<!ENTITY" in sample:
                raise PrusaNativeError(f"{name} contains a forbidden XML declaration.")
            tail = sample[-16:]


@dataclass(frozen=True)
class Transform:
    values: tuple[float, ...] = IDENTITY_12

    @classmethod
    def parse(cls, text: str | None) -> "Transform":
        if not text or not text.strip():
            return cls()
        parts = text.replace(",", " ").split()
        if len(parts) != 12:
            raise PrusaNativeError(f"3MF transform must contain 12 numbers: {text!r}")
        try:
            return cls(tuple(float(value) for value in parts))
        except ValueError as exc:
            raise PrusaNativeError(f"Invalid 3MF transform: {text!r}") from exc

    def compose(self, parent: "Transform") -> "Transform":
        def rows(value: tuple[float, ...]):
            return (
                (value[0], value[1], value[2], 0.0),
                (value[3], value[4], value[5], 0.0),
                (value[6], value[7], value[8], 0.0),
                (value[9], value[10], value[11], 1.0),
            )

        left, right = rows(self.values), rows(parent.values)
        return Transform(tuple(
            sum(left[row][k] * right[k][column] for k in range(4))
            for row in range(4)
            for column in range(3)
        ))

    def apply(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        m = self.values
        return (
            x * m[0] + y * m[3] + z * m[6] + m[9],
            x * m[1] + y * m[4] + z * m[7] + m[10],
            x * m[2] + y * m[5] + z * m[8] + m[11],
        )

    def is_identity(self, tolerance: float = 1e-9) -> bool:
        return all(abs(a - b) <= tolerance for a, b in zip(self.values, IDENTITY_12))

    def determinant(self) -> float:
        m = self.values
        return (
            m[0] * (m[4] * m[8] - m[5] * m[7])
            - m[1] * (m[3] * m[8] - m[5] * m[6])
            + m[2] * (m[3] * m[7] - m[4] * m[6])
        )


@dataclass
class PartSettings:
    object_id: str
    name: str = ""
    extruder: int | None = None


@dataclass
class ObjectSettings:
    object_id: str
    name: str = ""
    extruder: int | None = None
    parts: list[PartSettings] = field(default_factory=list)


@dataclass(frozen=True)
class Component:
    object_id: str
    path: str | None
    transform: Transform


@dataclass
class ObjectDefinition:
    object_id: str
    name: str = ""
    has_mesh: bool = False
    components: list[Component] = field(default_factory=list)


@dataclass(frozen=True)
class BuildItem:
    object_id: str
    transform: str
    printable: bool


@dataclass(frozen=True)
class Leaf:
    path: str
    object_id: str
    transform: Transform


@dataclass
class Volume:
    first_triangle: int
    last_triangle: int
    name: str
    extruder: int | None


@dataclass
class OutputObject:
    source_id: str
    output_id: str
    name: str
    extruder: int | None
    leaves: list[Leaf]
    part_settings: list[PartSettings]
    volumes: list[Volume] = field(default_factory=list)


@dataclass
class Project:
    archive: zipfile.ZipFile
    definitions: dict[tuple[str, str], ObjectDefinition]
    build_items: list[BuildItem]
    settings: dict[str, ObjectSettings]


def _normalize_path(target: str, current_path: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(current_path), target))


def _production_path(attributes: dict[str, str]) -> str | None:
    return next((value for key, value in attributes.items() if _local(key) == "path"), None)


def _read_settings(archive: zipfile.ZipFile) -> dict[str, ObjectSettings]:
    if MODEL_SETTINGS not in archive.namelist():
        raise PrusaNativeError(f"Missing {MODEL_SETTINGS}.")
    root = _parse_xml(archive.read(MODEL_SETTINGS), MODEL_SETTINGS)
    result: dict[str, ObjectSettings] = {}
    for element in root:
        if _local(element.tag) != "object" or not element.get("id"):
            continue
        meta = _metadata(element)
        try:
            extruder = int(meta["extruder"]) if meta.get("extruder") else None
        except ValueError as exc:
            raise PrusaNativeError("Invalid object extruder in model_settings.config.") from exc
        settings = ObjectSettings(element.get("id", ""), meta.get("name", ""), extruder)
        for part in element:
            if _local(part.tag) != "part":
                continue
            part_meta = _metadata(part)
            try:
                part_extruder = int(part_meta["extruder"]) if part_meta.get("extruder") else None
            except ValueError as exc:
                raise PrusaNativeError("Invalid part extruder in model_settings.config.") from exc
            settings.parts.append(PartSettings(part.get("id", ""), part_meta.get("name", ""), part_extruder))
        result[settings.object_id] = settings
    return result


def _catalog_file(archive: zipfile.ZipFile, path: str) -> dict[str, ObjectDefinition]:
    if path not in archive.namelist():
        raise PrusaNativeError(f"Referenced 3MF model member is missing: {path}")
    _assert_safe_member(archive, path)
    definitions: dict[str, ObjectDefinition] = {}
    current: ObjectDefinition | None = None
    with archive.open(path) as stream:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            tag = _local(element.tag)
            if event == "start" and tag == "object":
                current = ObjectDefinition(element.get("id", ""), element.get("name", ""))
            elif event == "end" and current is not None:
                if tag == "mesh":
                    current.has_mesh = True
                elif tag == "component":
                    current.components.append(Component(
                        element.get("objectid", ""),
                        _production_path(element.attrib),
                        Transform.parse(element.get("transform")),
                    ))
                elif tag == "object":
                    if not current.object_id:
                        raise PrusaNativeError(f"Object without id in {path}.")
                    definitions[current.object_id] = current
                    current = None
            if event == "end":
                element.clear()
    return definitions


def _read_project(archive: zipfile.ZipFile) -> Project:
    if ROOT_MODEL not in archive.namelist():
        raise PrusaNativeError(f"Missing {ROOT_MODEL}.")
    root = _parse_xml(archive.read(ROOT_MODEL), ROOT_MODEL)
    build_items = []
    for element in root.iter():
        if _local(element.tag) == "item" and element.get("objectid"):
            build_items.append(BuildItem(
                element.get("objectid", ""),
                element.get("transform", ""),
                element.get("printable", "1").lower() not in {"0", "false"},
            ))
    if not build_items:
        raise PrusaNativeError("The Bambu project does not contain build items.")

    definitions: dict[tuple[str, str], ObjectDefinition] = {}
    pending = deque([ROOT_MODEL])
    seen: set[str] = set()
    while pending:
        path = pending.popleft()
        if path in seen:
            continue
        seen.add(path)
        for object_id, definition in _catalog_file(archive, path).items():
            definitions[(path, object_id)] = definition
            for component in definition.components:
                target_path = _normalize_path(component.path, path) if component.path else path
                if target_path not in seen:
                    pending.append(target_path)
    return Project(archive, definitions, build_items, _read_settings(archive))


def _flatten(project: Project, key: tuple[str, str], transform: Transform | None = None, stack: tuple = ()) -> list[Leaf]:
    if key in stack:
        raise PrusaNativeError(f"Cyclic component reference at {key[0]} object {key[1]}.")
    if len(stack) >= 32:
        raise PrusaNativeError("Component graph is too deep.")
    definition = project.definitions.get(key)
    if definition is None:
        raise PrusaNativeError(f"Referenced object is missing: {key[0]} object {key[1]}.")
    cumulative = transform or Transform()
    leaves = [Leaf(key[0], key[1], cumulative)] if definition.has_mesh else []
    for component in definition.components:
        path = _normalize_path(component.path, key[0]) if component.path else key[0]
        leaves.extend(_flatten(
            project,
            (path, component.object_id),
            component.transform.compose(cumulative),
            stack + (key,),
        ))
    return leaves


def _iter_member_elements(archive: zipfile.ZipFile, path: str, object_id: str, wanted: str) -> Iterator[ET.Element]:
    current_id: str | None = None
    with archive.open(path) as stream:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            tag = _local(element.tag)
            if event == "start" and tag == "object":
                current_id = element.get("id")
            elif event == "end":
                if current_id == object_id and tag == wanted:
                    yield element
                if tag == "object":
                    current_id = None
                element.clear()


def _format_float(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _hash_vertex(digest, xyz: tuple[float, float, float]) -> None:
    digest.update(struct.pack("!ddd", *xyz))


def _hash_triangle(digest, indices: tuple[int, int, int]) -> None:
    digest.update(struct.pack("!QQQ", *indices))


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _mapped_tool(tool: int | None, mapping: dict[int, int]) -> int | None:
    if tool is None:
        return None
    if tool not in mapping:
        raise PrusaNativeError(f"Tool {tool} does not have an XL mapping.")
    return mapping[tool]


def _part_for(parts: list[PartSettings], leaf_index: int, leaf_id: str, leaf_count: int) -> PartSettings | None:
    by_id = {part.object_id: part for part in parts}
    if leaf_id in by_id:
        return by_id[leaf_id]
    if len(parts) == leaf_count:
        return parts[leaf_index]
    return None


def _cfg_metadata(parent: ET.Element, metadata_type: str, key: str, value: object) -> None:
    ET.SubElement(parent, "metadata", {"type": metadata_type, "key": key, "value": str(value)})


def _model_config(objects: list[OutputObject], instance_counts: Counter) -> bytes:
    root = ET.Element("config")
    for output in objects:
        object_element = ET.SubElement(root, "object", {
            "id": output.output_id,
            "instances_count": str(instance_counts[output.source_id]),
        })
        if output.name:
            _cfg_metadata(object_element, "object", "name", output.name)
        if output.extruder is not None:
            _cfg_metadata(object_element, "object", "extruder", output.extruder)
        for volume in output.volumes:
            volume_element = ET.SubElement(object_element, "volume", {
                "firstid": str(volume.first_triangle),
                "lastid": str(volume.last_triangle),
            })
            _cfg_metadata(volume_element, "volume", "name", volume.name)
            _cfg_metadata(volume_element, "volume", "volume_type", "ModelPart")
            if volume.extruder is not None:
                _cfg_metadata(volume_element, "volume", "extruder", volume.extruder)
    ET.indent(root, space=" ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _thumbnail_members(archive: zipfile.ZipFile) -> tuple[list[str], str | None]:
    images = [name for name in archive.namelist() if name.lower().startswith("metadata/") and name.lower().endswith(".png")]
    if not images:
        return [], None
    def priority(name: str):
        low = name.lower()
        base = posixpath.basename(low)
        if base == "thumbnail.png": return (5, 0)
        if base == "plate_1.png": return (4, 0)
        if "thumbnail" in base: return (3, 0)
        if "preview" in base: return (2, 0)
        if base.startswith("plate_") and "small" not in base and "no_light" not in base: return (1, 0)
        return (0, 0)
    selected = max(images, key=priority)
    return images, selected


def _prepare_objects(project: Project, mapping: dict[int, int]) -> list[OutputObject]:
    output: list[OutputObject] = []
    seen: set[str] = set()
    for build in project.build_items:
        if build.object_id in seen:
            continue
        seen.add(build.object_id)
        leaves = _flatten(project, (ROOT_MODEL, build.object_id))
        if not leaves:
            raise PrusaNativeError(f"Build object {build.object_id} has no mesh geometry.")
        settings = project.settings.get(build.object_id, ObjectSettings(build.object_id))
        definition = project.definitions[(ROOT_MODEL, build.object_id)]
        output.append(OutputObject(
            build.object_id,
            str(len(output) + 1),
            settings.name or definition.name or f"Object {build.object_id}",
            _mapped_tool(settings.extruder, mapping),
            leaves,
            settings.parts,
        ))
    return output


def _write_model(project: Project, output_stream, objects: list[OutputObject], mapping: dict[int, int]) -> dict:
    source_to_output = {obj.source_id: obj.output_id for obj in objects}
    output_stream.write((
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE}" xmlns:slic3rpe="{SLIC3RPE}">\n'
        ' <metadata name="slic3rpe:Version3mf">1</metadata>\n'
        ' <metadata name="slic3rpe:MmPaintingVersion">1</metadata>\n'
        ' <metadata name="Application">3MF XL Tool Mapper</metadata>\n'
        ' <resources>\n'
    ).encode())

    paint_before: Counter = Counter()
    paint_after: Counter = Counter()
    changed_paint = 0
    painted_triangles = 0
    vertex_count = 0
    triangle_count = 0
    transformed_leaves = 0
    expected_vertex_hash = hashlib.sha256()
    expected_triangle_hash = hashlib.sha256()

    for output in objects:
        output_stream.write(f'  <object id="{output.output_id}" type="model" name="{_xml_attr(output.name)}">\n   <mesh>\n    <vertices>\n'.encode())
        vertex_bases: list[int] = []
        object_vertex_count = 0
        for leaf in output.leaves:
            vertex_bases.append(object_vertex_count)
            identity = leaf.transform.values == IDENTITY_12
            if not identity:
                transformed_leaves += 1
            leaf_vertices = 0
            for vertex in _iter_member_elements(project.archive, leaf.path, leaf.object_id, "vertex"):
                try:
                    source_xyz = tuple(float(vertex.get(axis, "0")) for axis in ("x", "y", "z"))
                except ValueError as exc:
                    raise PrusaNativeError(f"Invalid vertex in {leaf.path}.") from exc
                xyz = source_xyz if identity else leaf.transform.apply(*source_xyz)
                values = tuple(vertex.get(axis, "0") for axis in ("x", "y", "z")) if identity else tuple(_format_float(v) for v in xyz)
                output_stream.write(f'     <vertex x="{values[0]}" y="{values[1]}" z="{values[2]}"/>\n'.encode())
                _hash_vertex(expected_vertex_hash, xyz)
                leaf_vertices += 1
            if leaf_vertices == 0:
                raise PrusaNativeError(f"Mesh object {leaf.object_id} in {leaf.path} has no vertices.")
            object_vertex_count += leaf_vertices
        vertex_count += object_vertex_count
        output_stream.write(b"    </vertices>\n    <triangles>\n")

        object_triangle_count = 0
        for leaf_index, leaf in enumerate(output.leaves):
            first_triangle = object_triangle_count
            flip = leaf.transform.determinant() < 0
            for triangle in _iter_member_elements(project.archive, leaf.path, leaf.object_id, "triangle"):
                try:
                    local_indices = tuple(int(triangle.get(key, "")) for key in ("v1", "v2", "v3"))
                except ValueError as exc:
                    raise PrusaNativeError(f"Invalid triangle in {leaf.path}.") from exc
                if flip:
                    local_indices = (local_indices[1], local_indices[0], local_indices[2])
                indices = tuple(value + vertex_bases[leaf_index] for value in local_indices)
                paint = triangle.get("paint_color")
                paint_attr = ""
                if paint:
                    try:
                        decoded = decode_paint_color(paint)
                        if encode_paint_color(decoded) != paint:
                            raise PrusaNativeError("TriangleSelector identity round-trip failed.")
                        source_states = list(paint_states(decoded))
                        missing = sorted({state for state in source_states if state and state not in mapping})
                        if missing:
                            raise PrusaNativeError(f"Painted Tool {missing[0]} does not have an XL mapping.")
                        remapped = remap_paint_color(decoded, mapping)
                        encoded = encode_paint_color(remapped)
                    except PaintCodecError as exc:
                        raise PrusaNativeError(f"Unreadable paint_color {paint!r} in {leaf.path}.") from exc
                    paint_before.update(source_states)
                    paint_after.update(paint_states(remapped))
                    changed_paint += encoded != paint
                    painted_triangles += 1
                    paint_attr = f' slic3rpe:mmu_segmentation="{encoded}"'
                output_stream.write(f'     <triangle v1="{indices[0]}" v2="{indices[1]}" v3="{indices[2]}"{paint_attr}/>\n'.encode())
                _hash_triangle(expected_triangle_hash, indices)
                object_triangle_count += 1
            if object_triangle_count == first_triangle:
                raise PrusaNativeError(f"Mesh object {leaf.object_id} in {leaf.path} has no triangles.")
            part = _part_for(output.part_settings, leaf_index, leaf.object_id, len(output.leaves))
            definition = project.definitions[(leaf.path, leaf.object_id)]
            output.volumes.append(Volume(
                first_triangle,
                object_triangle_count - 1,
                (part.name if part else "") or definition.name or output.name,
                _mapped_tool(part.extruder, mapping) if part else None,
            ))
        triangle_count += object_triangle_count
        output_stream.write(b"    </triangles>\n   </mesh>\n  </object>\n")

    output_stream.write(b" </resources>\n <build>\n")
    for item in project.build_items:
        output_id = source_to_output[item.object_id]
        transform = f' transform="{_xml_attr(item.transform)}"' if item.transform.strip() else ""
        printable = "1" if item.printable else "0"
        output_stream.write(f'  <item objectid="{output_id}"{transform} printable="{printable}"/>\n'.encode())
    output_stream.write(b" </build>\n</model>\n")
    return {
        "paint_states_before": dict(sorted(paint_before.items())),
        "paint_states_after": dict(sorted(paint_after.items())),
        "paint_values_changed": changed_paint,
        "painted_triangles": painted_triangles,
        "vertices": vertex_count,
        "triangles": triangle_count,
        "component_transforms_baked": transformed_leaves,
        "expected_vertex_sha256": expected_vertex_hash.hexdigest(),
        "expected_triangle_sha256": expected_triangle_hash.hexdigest(),
    }


def _validate_output(path: Path, objects: list[OutputObject], build_items: list[BuildItem], write_stats: dict) -> dict:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise PrusaNativeError("Generated Prusa 3MF ZIP is damaged.")
        names = archive.namelist()
        required = {"[Content_Types].xml", "_rels/.rels", ROOT_MODEL, MODEL_CONFIG}
        if not required.issubset(names):
            raise PrusaNativeError("Generated package is missing a required Prusa 3MF member.")
        forbidden = {MODEL_SETTINGS, "Metadata/project_settings.config"}
        if forbidden & set(names) or any(name.startswith("3D/Objects/") for name in names):
            raise PrusaNativeError("Generated package still contains Bambu project structure.")

        vertex_hash = hashlib.sha256()
        triangle_hash = hashlib.sha256()
        vertex_count = triangle_count = painted_count = object_count = 0
        paint_states_output: Counter = Counter()
        output_build: list[tuple[str, str, str]] = []
        with archive.open(ROOT_MODEL) as stream:
            for event, element in ET.iterparse(stream, events=("start", "end")):
                tag = _local(element.tag)
                if event == "start" and tag == "object":
                    object_count += 1
                elif event == "end" and tag == "vertex":
                    xyz = tuple(float(element.get(axis, "0")) for axis in ("x", "y", "z"))
                    _hash_vertex(vertex_hash, xyz)
                    vertex_count += 1
                elif event == "end" and tag == "triangle":
                    if "paint_color" in element.attrib:
                        raise PrusaNativeError("Generated Prusa model still contains paint_color.")
                    indices = tuple(int(element.get(key, "")) for key in ("v1", "v2", "v3"))
                    _hash_triangle(triangle_hash, indices)
                    paint = element.get(f"{{{SLIC3RPE}}}mmu_segmentation")
                    if paint:
                        decoded = decode_paint_color(paint)
                        if encode_paint_color(decoded) != paint:
                            raise PrusaNativeError("Generated mmu_segmentation failed identity round-trip.")
                        paint_states_output.update(paint_states(decoded))
                        painted_count += 1
                    triangle_count += 1
                elif event == "end" and tag == "item":
                    output_build.append((element.get("objectid", ""), element.get("transform", ""), element.get("printable", "1")))
                if event == "end":
                    element.clear()

        if object_count != len(objects) or vertex_count != write_stats["vertices"] or triangle_count != write_stats["triangles"]:
            raise PrusaNativeError("Generated Prusa geometry counts do not match the source conversion.")
        if vertex_hash.hexdigest() != write_stats["expected_vertex_sha256"] or triangle_hash.hexdigest() != write_stats["expected_triangle_sha256"]:
            raise PrusaNativeError("Generated Prusa geometry differs from the flattened source geometry.")
        if dict(sorted(paint_states_output.items())) != write_stats["paint_states_after"] or painted_count != write_stats["painted_triangles"]:
            raise PrusaNativeError("Generated Prusa painting differs from the mapped source painting.")
        expected_build = [
            (next(obj.output_id for obj in objects if obj.source_id == item.object_id), item.transform, "1" if item.printable else "0")
            for item in build_items
        ]
        if output_build != expected_build:
            raise PrusaNativeError("Generated Prusa build transforms differ from the Bambu source.")

        config = _parse_xml(archive.read(MODEL_CONFIG), MODEL_CONFIG)
        configured_objects = [element for element in config if _local(element.tag) == "object"]
        if len(configured_objects) != len(objects):
            raise PrusaNativeError("Prusa model config object count does not match the model.")
        config_tools = []
        volume_count = 0
        for element in configured_objects:
            meta = _metadata(element)
            if meta.get("extruder"):
                config_tools.append(int(meta["extruder"]))
            volumes = [child for child in element if _local(child.tag) == "volume"]
            volume_count += len(volumes)
            expected_object = next(obj for obj in objects if obj.output_id == element.get("id"))
            actual_ranges = [(int(volume.get("firstid", "-1")), int(volume.get("lastid", "-1"))) for volume in volumes]
            expected_ranges = [(volume.first_triangle, volume.last_triangle) for volume in expected_object.volumes]
            if actual_ranges != expected_ranges:
                raise PrusaNativeError("Prusa volume triangle ranges differ from flattened source leaves.")
            for volume in volumes:
                vmeta = _metadata(volume)
                if vmeta.get("volume_type") != "ModelPart":
                    raise PrusaNativeError("Prusa volume_type is not ModelPart.")
                if int(volume.get("firstid", "-1")) > int(volume.get("lastid", "-1")):
                    raise PrusaNativeError("Invalid Prusa volume triangle range.")
        if volume_count != sum(len(obj.volumes) for obj in objects):
            raise PrusaNativeError("Prusa volume count does not match flattened leaves.")
    return {
        "prusa_parse_ok": True,
        "objects": object_count,
        "volumes": volume_count,
        "build_items": len(output_build),
        "tools": config_tools,
        "output_vertex_sha256": vertex_hash.hexdigest(),
        "output_triangle_sha256": triangle_hash.hexdigest(),
        "build_transforms_preserved": True,
        "output_members": names,
    }


def export_prusa_native(source: Path, destination: Path, mapping: dict[int, int], original_hash: str) -> dict:
    try:
        with zipfile.ZipFile(source) as source_archive:
            project = _read_project(source_archive)
            objects = _prepare_objects(project, mapping)
            instance_counts = Counter(item.object_id for item in project.build_items)
            images, selected_thumbnail = _thumbnail_members(source_archive)
            with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
                output.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
                output.writestr("_rels/.rels", RELS_XML)
                with output.open(ROOT_MODEL, "w", force_zip64=True) as model_stream:
                    write_stats = _write_model(project, model_stream, objects, mapping)
                output.writestr(MODEL_CONFIG, _model_config(objects, instance_counts))
                for name in images:
                    output.writestr(name, source_archive.read(name))
                if selected_thumbnail and "Metadata/thumbnail.png" not in images:
                    output.writestr("Metadata/thumbnail.png", source_archive.read(selected_thumbnail))
        validation = _validate_output(destination, objects, project.build_items, write_stats)
        report = {
            "format": "prusa-native",
            "original_sha256_before": original_hash,
            "original_sha256_after": _sha256_path(source),
            "mapping_before_extruder": dict(sorted(mapping.items())),
            "mapping_before_paint": dict(sorted(mapping.items())),
            **write_stats,
            **validation,
        }
        if report["original_sha256_after"] != original_hash:
            raise PrusaNativeError("Original 3MF changed during export.")
        return report
    except (zipfile.BadZipFile, OSError, ET.ParseError) as exc:
        raise PrusaNativeError(f"Cannot convert Bambu 3MF to Prusa format: {exc}") from exc
