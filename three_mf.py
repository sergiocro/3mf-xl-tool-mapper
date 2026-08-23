from __future__ import annotations

import hashlib
import json
import logging
import re
import zipfile
from typing import Callable
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree as ET

from paint_codec import PaintCodecError, decode_paint_color, encode_paint_color, paint_states, remap_paint_color
from prusa_native import PrusaNativeError, export_prusa_native

MODEL_SETTINGS = "Metadata/model_settings.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"
EXTRUDER_RE = re.compile(rb'(<metadata\b(?=[^>]*\bkey\s*=\s*(["\'])extruder\2)[^>]*?\bvalue\s*=\s*)(["\'])(\d+)(\3)', re.I)
PAINT_COLOR_RE = re.compile(rb'(\bpaint_color\s*=\s*)(["\'])([0-9A-Fa-f]+)(\2)')
logger = logging.getLogger("3mf-tool-mapper")
NON_FILAMENT_SLOT_ARRAYS = {
    "bed_exclude_area",
    "print_compatible_printers",
    "printable_area",
}


class ThreeMFError(ValueError):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    object_id: str
    name: str
    tool: int


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_xml(data: bytes, label: str) -> ET.Element:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ThreeMFError(f"{label} sadrži nedopuštenu XML deklaraciju.")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ThreeMFError(f"{label} sadrži nečitljiv XML: {exc}") from exc


def _metadata(parent: ET.Element) -> dict[str, str]:
    return {e.get("key", ""): e.get("value", "") for e in parent if e.tag.rsplit("}", 1)[-1] == "metadata" and e.get("key")}


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _select_thumbnail(archive: zipfile.ZipFile) -> dict | None:
    referenced = set()
    if "3D/3dmodel.model" in archive.namelist():
        try:
            model = _safe_xml(archive.read("3D/3dmodel.model"), "3D/3dmodel.model")
            for node in model.iter():
                if node.tag.rsplit("}", 1)[-1] == "metadata" and "thumbnail" in node.get("name", "").lower() and "small" not in node.get("name", "").lower():
                    if node.text:
                        referenced.add(node.text.strip().lstrip("/").lower())
        except ThreeMFError:
            pass
    candidates = []
    for name in archive.namelist():
        normalized = name.replace("\\", "/")
        lower = normalized.lower()
        basename = lower.rsplit("/", 1)[-1]
        if not lower.startswith("metadata/") or not lower.endswith(".png"):
            continue
        if lower in referenced:
            priority = 5
        elif basename == "thumbnail.png":
            priority = 4
        elif "thumbnail" in basename:
            priority = 3
        elif "preview" in basename:
            priority = 2
        elif basename.startswith("plate_"):
            priority = 1
        else:
            continue
        size = _png_size(archive.read(name))
        if size:
            candidates.append((priority, size[0] * size[1], size[0], size[1], name))
    if not candidates:
        return None
    _, _, width, height, member = max(candidates)
    return {"member": member, "width": width, "height": height, "mime": "image/png"}


def inspect_archive(source: str | Path | BinaryIO) -> dict:
    try:
        with zipfile.ZipFile(source) as archive:
            if archive.testzip() is not None:
                raise ThreeMFError("3MF ZIP arhiva je oštećena.")
            names = archive.namelist()
            if MODEL_SETTINGS not in names:
                raise ThreeMFError(f"Nedostaje {MODEL_SETTINGS}.")
            data = archive.read(MODEL_SETTINGS)
            root = _safe_xml(data, MODEL_SETTINGS)
            objects: list[ObjectInfo] = []
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] != "object":
                    continue
                meta = _metadata(node)
                if "extruder" in meta:
                    try:
                        tool = int(meta["extruder"])
                    except ValueError as exc:
                        raise ThreeMFError("Pronađen je nevalidan Tool broj.") from exc
                    objects.append(ObjectInfo(node.get("id", "—"), meta.get("name", "—"), tool))
            if not objects:
                raise ThreeMFError('Nije pronađen metadata key="extruder" na objektima.')
            project = {}
            if PROJECT_SETTINGS in names:
                try:
                    project = json.loads(archive.read(PROJECT_SETTINGS).decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    project = {}
            tools = sorted({obj.tool for obj in objects})
            colours = project.get("filament_colour")
            configured_tools = list(range(1, len(colours) + 1)) if isinstance(colours, list) else []
            display_tool_numbers = configured_tools
            filament_tool_numbers = sorted(set(display_tool_numbers) | set(tools))
            filaments = {str(t): _filament(project, t) for t in filament_tool_numbers}
            display_tools = [
                {
                    "tool": t,
                    **filaments[str(t)],
                }
                for t in display_tool_numbers
            ]
            return {
                "objects": [obj.__dict__ for obj in objects],
                "object_count": len(objects),
                "used_tools": tools,
                "display_tools": display_tools,
                "tool_counts": dict(Counter(obj.tool for obj in objects)),
                "filaments": filaments,
                "thumbnail": _select_thumbnail(archive),
                "members": names,
            }
    except (zipfile.BadZipFile, OSError) as exc:
        raise ThreeMFError("Datoteka nije valjana ZIP/3MF arhiva.") from exc


def _at(value, index: int):
    if isinstance(value, list) and 0 <= index < len(value):
        return value[index]
    return None


def _filament(project: dict, tool: int) -> dict:
    i = tool - 1
    fields = {
        "color": ("filament_colour", "#808080"),
        "type": ("filament_type", None),
        "vendor": ("filament_vendor", None),
        "profile": ("filament_settings_id", None),
        "filament_id": ("filament_ids", None),
    }
    result = {}
    for target, (key, fallback) in fields.items():
        value = _at(project.get(key), i)
        result[target] = value if value not in (None, "") else fallback
    return result


def validate_mapping(used_tools: list[int], mapping: dict[int, int], confirm_conflict: bool) -> None:
    if not set(used_tools).issubset(mapping):
        raise ThreeMFError("Svi korišteni originalni Toolovi moraju imati mapiranje.")
    if any(not isinstance(v, int) or not 1 <= v <= 5 for v in mapping.values()):
        raise ThreeMFError("XL Tool mora biti cijeli broj od 1 do 5.")
    if len(set(mapping.values())) != len(mapping) and not confirm_conflict:
        raise ThreeMFError("Više originalnih Toolova koristi isti XL Tool; potrebna je eksplicitna potvrda.")


def _replace_extruders(data: bytes, mapping: dict[int, int]) -> tuple[bytes, list[int]]:
    mapping_snapshot = dict(mapping)
    values: list[int] = []
    def replace(match: re.Match[bytes]) -> bytes:
        old = int(match.group(4))
        if old not in mapping_snapshot:
            raise ThreeMFError(f"Tool {old} nema mapiranje.")
        new = mapping_snapshot[old]
        if mapping != mapping_snapshot or mapping.get(old) != new:
            raise ThreeMFError(f"Mapping se promijenio prije upisa: Tool {old}.")
        logger.info("model_settings write assertion: source_tool=%s -> target_tool=%s", old, new)
        values.append(new)
        return match.group(1) + match.group(3) + str(new).encode("ascii") + match.group(5)
    result = EXTRUDER_RE.sub(replace, data)
    if not values:
        raise ThreeMFError('Nije pronađen metadata key="extruder".')
    return result, values


def _replace_project_filaments(data: bytes, mapping: dict[int, int]) -> tuple[bytes, list[str]]:
    try:
        project = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return data, []
    colours = project.get("filament_colour")
    if not isinstance(colours, list) or not colours:
        return data, []
    source_count = len(colours)
    invalid_sources = sorted(source for source in mapping if source < 1 or source > source_count)
    if invalid_sources:
        raise ThreeMFError(f"Project metadata nema filament slot Tool {invalid_sources[0]}.")
    target_count = max(source_count, max(mapping.values(), default=source_count))
    destination_sources = list(range(source_count))
    destination_sources.extend([source_count - 1] * (target_count - source_count))
    for source_tool, target_tool in sorted(mapping.items()):
        destination_sources[target_tool - 1] = source_tool - 1

    changed_fields: list[str] = []
    original = {key: list(value) for key, value in project.items() if isinstance(value, list)}
    for key, value in original.items():
        if key in NON_FILAMENT_SLOT_ARRAYS or len(value) != source_count:
            continue
        project[key] = [value[source] for source in destination_sources]
        changed_fields.append(key)

    self_index = original.get("filament_self_index")
    if isinstance(self_index, list) and len(self_index) == source_count:
        as_text = all(isinstance(value, str) for value in self_index)
        project["filament_self_index"] = [str(i) if as_text else i for i in range(1, target_count + 1)]

    matrix = original.get("flush_volumes_matrix")
    if isinstance(matrix, list) and len(matrix) == source_count * source_count:
        project["flush_volumes_matrix"] = [
            matrix[row_source * source_count + column_source]
            for row_source in destination_sources
            for column_source in destination_sources
        ]
        changed_fields.append("flush_volumes_matrix")

    vector = original.get("flush_volumes_vector")
    if isinstance(vector, list) and len(vector) == source_count * 2:
        unload = vector[:source_count]
        load = vector[source_count:]
        project["flush_volumes_vector"] = (
            [unload[source] for source in destination_sources]
            + [load[source] for source in destination_sources]
        )
        changed_fields.append("flush_volumes_vector")

    differences = original.get("different_settings_to_system")
    if isinstance(differences, list) and len(differences) == source_count + 2:
        project["different_settings_to_system"] = (
            [differences[0]]
            + [differences[1 + source] for source in destination_sources]
            + [differences[-1]]
        )
        changed_fields.append("different_settings_to_system")

    if all(project.get(key) == value for key, value in original.items()):
        return data, []
    newline = "\r\n" if b"\r\n" in data else "\n"
    trailing_newline = data.endswith((b"\r\n", b"\n"))
    rendered = json.dumps(project, ensure_ascii=True, indent=4)
    if newline != "\n":
        rendered = rendered.replace("\n", newline)
    if trailing_newline:
        rendered += newline
    prefix = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    return prefix + rendered.encode("utf-8"), sorted(set(changed_fields))


def _decode_paint_values(data: bytes) -> list[tuple[bytes, object]]:
    decoded = []
    for match in PAINT_COLOR_RE.finditer(data):
        raw = match.group(3)
        try:
            code = decode_paint_color(raw.decode("ascii"))
        except (UnicodeDecodeError, PaintCodecError) as exc:
            raise ThreeMFError(f"Nečitljiv paint_color: {raw[:80]!r}") from exc
        if encode_paint_color(code).encode("ascii") != raw:
            raise ThreeMFError("paint_color codec nije prošao identičan round-trip; export je zaustavljen.")
        decoded.append((raw, code))
    return decoded


def _member_contains_paint(archive: zipfile.ZipFile, name: str) -> bool:
    tail = b""
    with archive.open(name) as stream:
        while chunk := stream.read(1024 * 1024):
            sample = tail + chunk
            if b"paint_color" in sample:
                return True
            tail = sample[-16:]
    return False


def _replace_paint_colors(data: bytes, mapping: dict[int, int]) -> tuple[bytes, Counter, Counter, int]:
    mapping_snapshot = dict(mapping)
    before = Counter()
    after = Counter()
    changed = 0

    def replace_value(match: re.Match[bytes]) -> bytes:
        nonlocal changed
        raw = match.group(3)
        try:
            code = decode_paint_color(raw.decode("ascii"))
        except (UnicodeDecodeError, PaintCodecError) as exc:
            raise ThreeMFError(f"Nečitljiv paint_color: {raw[:80]!r}") from exc
        encoded = encode_paint_color(code).encode("ascii")
        if encoded != raw:
            raise ThreeMFError("paint_color codec nije prošao identičan round-trip; export je zaustavljen.")
        before.update(paint_states(code))
        if mapping != mapping_snapshot:
            raise ThreeMFError("Mapping se promijenio prije paint_color upisa.")
        for source_tool, target_tool in mapping_snapshot.items():
            if mapping.get(source_tool) != target_tool:
                raise ThreeMFError(f"Mapping se promijenio prije upisa: Tool {source_tool}.")
        remapped = remap_paint_color(code, mapping_snapshot)
        after.update(paint_states(remapped))
        output = encode_paint_color(remapped).encode("ascii")
        changed += output != raw
        return match.group(1) + match.group(2) + output + match.group(4)

    output = PAINT_COLOR_RE.sub(replace_value, data)
    return output, before, after, changed


def export_archive(source: Path, destination: Path, mapping: dict[int, int], confirm_conflict: bool = False, check_cancel: Callable[[], None] | None = None) -> dict:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ThreeMFError("Originalni 3MF nije dopušteno prepisati.")
    original_hash = sha256_path(source)
    info = inspect_archive(source)
    validate_mapping(info["used_tools"], mapping, confirm_conflict)
    if destination.exists():
        raise ThreeMFError("Odredišna datoteka već postoji; odaberite novi naziv.")
    with zipfile.ZipFile(source, "r") as archive:
        painted_bambu = any(
            name.startswith("3D/")
            and name.lower().endswith(".model")
            and _member_contains_paint(archive, name)
            for name in archive.namelist()
        )
    if painted_bambu:
        try:
            result = export_prusa_native(source, destination, mapping, original_hash, check_cancel)
            result["generated_sha256"] = sha256_path(destination)
            result["paint_roundtrip_ok"] = True
            result["project_filament_fields_changed"] = []
            return result
        except PrusaNativeError as exc:
            destination.unlink(missing_ok=True)
            raise ThreeMFError(str(exc)) from exc
    paint_members: dict[str, list[tuple[bytes, object]]] = {}
    with zipfile.ZipFile(source, "r") as archive:
        for name in archive.namelist():
            if name.startswith("3D/") and name.lower().endswith(".model"):
                decoded = _decode_paint_values(archive.read(name))
                if decoded:
                    paint_members[name] = decoded
    unique_paint = {raw for values in paint_members.values() for raw, _ in values}
    try:
        paint_before = Counter()
        paint_after = Counter()
        changed_paint_values = 0
        project_fields_changed: list[str] = []
        logger.info("Mapping immediately before extruder remap: %s", dict(sorted(mapping.items())))
        with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(destination, "x") as zout:
            for item in zin.infolist():
                payload = zin.read(item.filename)
                if item.filename == MODEL_SETTINGS:
                    payload, _ = _replace_extruders(payload, mapping)
                elif item.filename == PROJECT_SETTINGS:
                    payload, project_fields_changed = _replace_project_filaments(payload, mapping)
                elif item.filename in paint_members:
                    if not paint_before:
                        logger.info("Mapping immediately before paint_color remap: %s", dict(sorted(mapping.items())))
                    payload, before, after, changed = _replace_paint_colors(payload, mapping)
                    paint_before.update(before)
                    paint_after.update(after)
                    changed_paint_values += changed
                zout.writestr(item, payload)
        result = verify_export(source, destination, mapping, original_hash)
        result["generated_sha256"] = sha256_path(destination)
        result["paint_roundtrip_unique"] = len(unique_paint)
        result["paint_roundtrip_ok"] = True
        result["paint_states_before"] = dict(sorted(paint_before.items()))
        result["paint_states_after"] = dict(sorted(paint_after.items()))
        result["paint_values_changed"] = changed_paint_values
        result["project_filament_fields_changed"] = project_fields_changed
        result["mapping_before_extruder"] = dict(sorted(mapping.items()))
        result["mapping_before_paint"] = dict(sorted(mapping.items()))
        return result
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def verify_export(source: Path, destination: Path, mapping: dict[int, int], original_hash: str) -> dict:
    if sha256_path(source) != original_hash:
        raise ThreeMFError("Originalna datoteka promijenjena je tijekom izvoza.")
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(destination) as after:
        if after.testzip() is not None:
            raise ThreeMFError("Generirana ZIP arhiva nije valjana.")
        if before.namelist() != after.namelist():
            raise ThreeMFError("Popis ZIP članova nije očuvan.")
        for name in before.namelist():
            original = before.read(name)
            generated = after.read(name)
            if name == MODEL_SETTINGS:
                continue
            if name == PROJECT_SETTINGS:
                expected_project, _ = _replace_project_filaments(original, mapping)
                if generated != expected_project:
                    raise ThreeMFError("project_settings filament metadata validacija nije uspjela.")
                continue
            if name.startswith("3D/") and name.lower().endswith(".model"):
                if PAINT_COLOR_RE.sub(rb'\1\2\2', original) != PAINT_COLOR_RE.sub(rb'\1\2\2', generated):
                    raise ThreeMFError(f"Nedopuštena promjena izvan paint_color vrijednosti: {name}")
                original_values = [code for _, code in _decode_paint_values(original)]
                generated_values = [code for _, code in _decode_paint_values(generated)]
                if len(original_values) != len(generated_values):
                    raise ThreeMFError(f"Promijenjen broj paint_color atributa: {name}")
                for old_code, new_code in zip(original_values, generated_values):
                    expected = list(paint_states(remap_paint_color(old_code, mapping)))
                    if list(paint_states(new_code)) != expected:
                        raise ThreeMFError(f"paint_color post-export validacija nije uspjela: {name}")
            elif original != generated:
                raise ThreeMFError(f"Nedopuštena promjena ZIP člana: {name}")
        old_root = _safe_xml(before.read(MODEL_SETTINGS), MODEL_SETTINGS)
        new_root = _safe_xml(after.read(MODEL_SETTINGS), MODEL_SETTINGS)
        old_values = [int(e.get("value", "0")) for e in old_root.iter() if e.get("key") == "extruder"]
        new_values = [int(e.get("value", "0")) for e in new_root.iter() if e.get("key") == "extruder"]
        expected = [mapping[v] for v in old_values]
        if new_values != expected or any(v not in range(1, 6) for v in new_values):
            raise ThreeMFError("Post-export Tool validacija nije uspjela.")
    return {"original_sha256_before": original_hash, "original_sha256_after": sha256_path(source), "tools": new_values}


def preview_mesh(source: Path, max_triangles: int = 120_000) -> dict:
    """Return a bounded, read-only preview. Export never uses this representation."""
    info = inspect_archive(source)
    vertices: list[list[float]] = []
    triangles: list[list[int]] = []
    colors: list[str] = []
    with zipfile.ZipFile(source) as archive:
        main = _safe_xml(archive.read("3D/3dmodel.model"), "3D/3dmodel.model")
        object_paths: dict[str, str] = {}
        transforms: dict[str, list[float]] = {}
        for node in main.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "object":
                component = next((c for c in node.iter() if c.tag.rsplit("}", 1)[-1] == "component"), None)
                if component is not None:
                    path = next((v for k, v in component.attrib.items() if k.rsplit("}", 1)[-1] == "path"), "")
                    object_paths[node.get("id", "")] = path.lstrip("/")
            elif tag == "item":
                transforms[node.get("objectid", "")] = [float(v) for v in node.get("transform", "1 0 0 0 1 0 0 0 1 0 0 0").split()]
        tools_by_id = {obj["object_id"]: obj["tool"] for obj in info["objects"]}
        for object_id, name in object_paths.items():
            if name not in archive.namelist() or object_id not in tools_by_id:
                continue
            root = _safe_xml(archive.read(name), name)
            local_vertices = []
            matrix = transforms.get(object_id, [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0])
            for e in root.iter():
                if e.tag.rsplit("}", 1)[-1] == "vertex":
                    x, y, z = [float(e.get(k, "0")) for k in ("x", "y", "z")]
                    local_vertices.append([
                        x * matrix[0] + y * matrix[3] + z * matrix[6] + matrix[9],
                        x * matrix[1] + y * matrix[4] + z * matrix[7] + matrix[10],
                        x * matrix[2] + y * matrix[5] + z * matrix[8] + matrix[11],
                    ])
            if not local_vertices:
                continue
            offset = len(vertices)
            vertices.extend(local_vertices)
            tool = tools_by_id[object_id]
            color = info["filaments"][str(tool)]["color"]
            for e in root.iter():
                if e.tag.rsplit("}", 1)[-1] == "triangle":
                    triangles.append([offset + int(e.get(k, "0")) for k in ("v1", "v2", "v3")])
                    colors.append(color)
    if len(triangles) > max_triangles:
        step = len(triangles) / max_triangles
        picks = [int(i * step) for i in range(max_triangles)]
        triangles = [triangles[i] for i in picks]
        colors = [colors[i] for i in picks]
    used = sorted({index for tri in triangles for index in tri})
    remap = {old: new for new, old in enumerate(used)}
    return {
        "vertices": [vertices[i] for i in used],
        "triangles": [[remap[i] for i in tri] for tri in triangles],
        "colors": colors,
    }
