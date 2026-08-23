from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from three_mf import ThreeMFError, export_archive, inspect_archive, preview_mesh, sha256_path
from prusa_native import PrusaNativeError

ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".work"
EXPORTS = ROOT / "exports"
WORK.mkdir(exist_ok=True)
EXPORTS.mkdir(exist_ok=True)

app = FastAPI(title="3MF XL Tool Mapper", docs_url=None, redoc_url=None)
logger = logging.getLogger("3mf-tool-mapper")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


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


def _perform_export(token: str, filename: str, mapping: str, confirm_conflict: bool, job_id: str | None = None):
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
        if job_id:
            with _jobs_lock:
                if _jobs[job_id]["cancel"]:
                    raise ThreeMFError("Konverzija prekinuta")
                _jobs[job_id].update(status="running", phase="Priprema modela", progress=10)
        raw = json.loads(mapping)
        logger.info("Export mapping received by FastAPI: %s", mapping)
        parsed = {int(k): int(v) for k, v in raw.items()}
        logger.info("Export mapping used by backend: %s", json.dumps(parsed, sort_keys=True))
        if job_id:
            with _jobs_lock:
                if _jobs[job_id]["cancel"]:
                    raise ThreeMFError("Konverzija prekinuta")
                _jobs[job_id].update(phase="Konverzija painting podataka", progress=30)
        def check_cancel():
            with _jobs_lock:
                if _jobs[job_id]["cancel"]:
                    raise PrusaNativeError("Konverzija prekinuta")
        report = export_archive(source, destination, parsed, confirm_conflict, check_cancel if job_id else None)
        report["mapping_request"] = raw
        report["mapping_normalized"] = dict(sorted(parsed.items()))
        report["mapping_used"] = dict(sorted(parsed.items()))
        backend_sha256 = sha256_path(destination)
        if backend_sha256 != report.get("generated_sha256"):
            raise ThreeMFError("Export artifact SHA-256 validacija nije uspjela.")
        report["export_id"] = export_id
        report["export_path"] = str(destination)
        report["export_sha256"] = backend_sha256
        if job_id:
            with _jobs_lock:
                if _jobs[job_id]["cancel"]:
                    raise ThreeMFError("Konverzija prekinuta")
                _jobs[job_id].update(phase="Validacija i spremanje", progress=100, status="completed", report=report, path=str(destination))
    except (ThreeMFError, PrusaNativeError, ValueError, json.JSONDecodeError) as exc:
        destination.unlink(missing_ok=True)
        export_directory.rmdir() if export_directory.exists() and not any(export_directory.iterdir()) else None
        if job_id:
            with _jobs_lock:
                cancelled = str(exc) == "Konverzija prekinuta"
                _jobs[job_id].update(status="cancelled" if cancelled else "error", phase="Konverzija prekinuta" if cancelled else "Greška", error=str(exc))
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


@app.post("/api/export")
def export(token: str = Form(...), filename: str = Form(...), mapping: str = Form(...), confirm_conflict: bool = Form(False)):
    return _perform_export(token, filename, mapping, confirm_conflict)


def _run_job(job_id: str, token: str, filename: str, mapping: str, confirm_conflict: bool):
    try:
        _perform_export(token, filename, mapping, confirm_conflict, job_id)
    except HTTPException:
        with _jobs_lock:
            if _jobs[job_id].get("status") != "cancelled":
                _jobs[job_id].update(status="error", phase="Greška", error="Učitana datoteka više nije dostupna.")


@app.post("/api/export/jobs")
def start_export_job(token: str = Form(...), filename: str = Form(...), mapping: str = Form(...), confirm_conflict: bool = Form(False)):
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued", "phase": "Priprema modela", "progress": 0, "started": time.monotonic(), "cancel": False}
    threading.Thread(target=_run_job, args=(job_id, token, filename, mapping, confirm_conflict), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/export/jobs/{job_id}")
def export_job_status(job_id: str):
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    if not job:
        raise HTTPException(404, "Nepoznat export posao.")
    elapsed = max(0, time.monotonic() - job.get("started", time.monotonic()))
    progress = job.get("progress", 0)
    job["elapsed"] = elapsed
    job["eta"] = (elapsed * (100 - progress) / progress) if progress >= 10 and progress < 100 else None
    return {k: v for k, v in job.items() if k not in {"started", "cancel"}}


@app.post("/api/export/jobs/{job_id}/cancel")
def cancel_export_job(job_id: str):
    with _jobs_lock:
        if job_id not in _jobs:
            raise HTTPException(404, "Nepoznat export posao.")
        current = _jobs[job_id]["status"]
        if current in {"queued", "running"}:
            _jobs[job_id]["cancel"] = True
            _jobs[job_id]["status"] = "cancelling"
            _jobs[job_id]["phase"] = "Prekid konverzije..."
        return {"status": _jobs[job_id]["status"]}


@app.get("/api/export/jobs/{job_id}/download")
def download_export_job(job_id: str):
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    if job.get("status") != "completed":
        raise HTTPException(409, "Export još nije završen.")
    path = Path(job["path"])
    report = job["report"]
    return FileResponse(path, media_type="model/3mf", filename=path.name, headers={
        "X-Export-ID": report["export_id"], "X-Export-SHA256": report["export_sha256"],
        "X-Export-Mapping": ",".join(f"{k}-{v}" for k, v in sorted(report["mapping_used"].items())),
        "X-Validation-Report": json.dumps(report, separators=(",", ":")), "Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8765")).start()
    uvicorn.run(app, host="127.0.0.1", port=8765)
