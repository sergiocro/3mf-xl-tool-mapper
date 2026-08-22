from __future__ import annotations

import json
import logging
import tempfile
import threading
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from three_mf import ThreeMFError, export_archive, inspect_archive, preview_mesh, sha256_path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".work"
EXPORTS = ROOT / "exports"
WORK.mkdir(exist_ok=True)
EXPORTS.mkdir(exist_ok=True)

app = FastAPI(title="3MF XL Tool Mapper", docs_url=None, redoc_url=None)
logger = logging.getLogger("3mf-tool-mapper")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def fail(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/api/inspect")
async def inspect(file: UploadFile = File(...)):
    suffix = Path(file.filename or "upload.3mf").suffix.lower()
    if suffix != ".3mf":
        raise HTTPException(400, "Odaberite .3mf datoteku.")
    token = next(tempfile._get_candidate_names())
    path = WORK / f"{token}.3mf"
    try:
        with path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
        result = inspect_archive(path)
        result.update({"token": token, "filename": Path(file.filename or "model.3mf").name, "sha256": sha256_path(path)})
        return result
    except ThreeMFError as exc:
        path.unlink(missing_ok=True)
        raise fail(exc)


@app.get("/api/preview/{token}")
def preview(token: str):
    try:
        return preview_mesh(WORK / f"{token}.3mf")
    except (ThreeMFError, OSError) as exc:
        raise fail(exc)


@app.get("/api/thumbnail/{token}")
def thumbnail(token: str):
    source = WORK / f"{token}.3mf"
    try:
        info = inspect_archive(source)
        selected = info["thumbnail"]
        if not selected:
            raise HTTPException(404, "Nema ugrađenog previewa.")
        import zipfile
        with zipfile.ZipFile(source) as archive:
            return Response(archive.read(selected["member"]), media_type=selected["mime"])
    except HTTPException:
        raise
    except (ThreeMFError, OSError) as exc:
        raise fail(exc)


@app.post("/api/export")
def export(token: str = Form(...), filename: str = Form(...), mapping: str = Form(...), confirm_conflict: bool = Form(False)):
    source = WORK / f"{token}.3mf"
    if not source.exists():
        raise HTTPException(404, "Učitana datoteka više nije dostupna.")
    safe_name = Path(filename).name
    if not safe_name.lower().endswith(".3mf"):
        safe_name += ".3mf"
    export_id = str(uuid.uuid4())
    export_directory = EXPORTS / export_id
    export_directory.mkdir(parents=True, exist_ok=False)
    destination = export_directory / safe_name
    try:
        raw = json.loads(mapping)
        logger.info("Export mapping received by FastAPI: %s", mapping)
        parsed = {int(k): int(v) for k, v in raw.items()}
        logger.info("Export mapping used by backend: %s", json.dumps(parsed, sort_keys=True))
        report = export_archive(source, destination, parsed, confirm_conflict)
        report["mapping_request"] = raw
        report["mapping_normalized"] = dict(sorted(parsed.items()))
        report["mapping_used"] = dict(sorted(parsed.items()))
        backend_sha256 = sha256_path(destination)
        if backend_sha256 != report.get("generated_sha256"):
            raise ThreeMFError("Export artifact SHA-256 validacija nije uspjela.")
        report["export_id"] = export_id
        report["export_path"] = str(destination)
        report["export_sha256"] = backend_sha256
    except (ThreeMFError, ValueError, json.JSONDecodeError) as exc:
        raise fail(exc)
    mapping_header = ",".join(f"{source}-{target}" for source, target in sorted(parsed.items()))
    headers = {
        "X-Validation-Report": json.dumps(report, separators=(",", ":")),
        "X-Export-ID": export_id,
        "X-Export-SHA256": backend_sha256,
        "X-Export-Mapping": mapping_header,
        "Cache-Control": "no-store",
    }
    return FileResponse(destination, media_type="model/3mf", filename=safe_name, headers=headers)


if __name__ == "__main__":
    import uvicorn
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8765")).start()
    uvicorn.run(app, host="127.0.0.1", port=8765)
