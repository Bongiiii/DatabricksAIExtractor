import os
import json
import uuid
import asyncio
import shutil
import traceback
import logging
import requests
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Dict, Any
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dataExtractor import EnhancedPDFExtractor, UC_VOLUME_PATH

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="PDF Data Extractor", version="3.0.0")

FRONTEND_URL = os.getenv("DATABRICKS_APP_URL", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

extractor = EnhancedPDFExtractor()
executor = ThreadPoolExecutor(max_workers=4)

# In-memory job store: job_id → {status, output_path, filename, error}
job_store: Dict[str, Any] = {}


def ensure_temp_dirs():
    upload_dir = "/tmp/uploaded"
    extract_dir = "/tmp/extracted_tables"
    os.makedirs(upload_dir, exist_ok=True)
    os.makedirs(extract_dir, exist_ok=True)
    return upload_dir, extract_dir


# ------------------------------------------------------------------ #
# Smoke-test endpoints                                                 #
# ------------------------------------------------------------------ #

@app.get("/test-vision")
async def test_vision(model: Optional[str] = None):
    test_model = model or extractor.vision_model
    try:
        tiny_png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
        )
        original = extractor.vision_model
        extractor.vision_model = test_model
        try:
            result = query_vision_model(tiny_png, "What do you see? Reply in one word.")
        finally:
            extractor.vision_model = original
        return {"status": "ok", "vision_model": test_model, "response": result}
    except requests.HTTPError as e:
        return JSONResponse(status_code=500, content={
            "status": "error",
            "vision_model": test_model,
            "http_status": e.response.status_code,
            "detail": e.response.text,
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "status": "error",
            "vision_model": test_model,
            "error": str(e),
        })


def query_vision_model(base64_image: str, prompt: str) -> str:
    workspace_host = extractor.w.config.host.rstrip("/")
    endpoint_url = f"{workspace_host}/serving-endpoints/{extractor.vision_model}/invocations"
    auth_headers = extractor.w.config.authenticate()
    headers = {**auth_headers, "Content-Type": "application/json"}
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}},
                ],
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.1,
    }
    resp = requests.post(endpoint_url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


@app.get("/health")
async def health_check():
    issues = []
    try:
        workspace_host = extractor.w.config.host
    except Exception as e:
        workspace_host = None
        issues.append(f"WorkspaceClient error: {e}")

    sql_http_path = os.getenv("DATABRICKS_SQL_HTTP_PATH")
    if not sql_http_path:
        issues.append("DATABRICKS_SQL_HTTP_PATH not set")

    try:
        import pytesseract
        tess_version = str(pytesseract.get_tesseract_version())
    except Exception as e:
        tess_version = None
        issues.append(f"Tesseract not available: {e}")

    volume_accessible = os.path.isdir(UC_VOLUME_PATH)
    if not volume_accessible:
        issues.append(f"UC Volume not accessible: {UC_VOLUME_PATH}")

    status = "healthy" if not issues else "degraded"
    response = {
        "status": status,
        "workspace": workspace_host,
        "sql_http_path_configured": bool(sql_http_path),
        "tesseract_version": tess_version,
        "uc_volume": UC_VOLUME_PATH,
        "uc_volume_accessible": volume_accessible,
        "models": {"vision": extractor.vision_model, "text": extractor.text_model},
        "extraction_methods": {
            "ai_parse_document": bool(sql_http_path),
            "tesseract_ocr": tess_version is not None,
            "vision_fallback": True,
        },
    }
    if issues:
        response["warnings"] = issues
    return JSONResponse(status_code=200 if status == "healthy" else 207, content=response)


@app.get("/test-sql")
async def test_sql_connection():
    try:
        conn = extractor._get_sql_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1 AS ping")
            row = cursor.fetchone()
        conn.close()
        return {"status": "ok", "message": "SQL Warehouse connection successful", "result": row[0] if row else None}
    except RuntimeError as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "status": "error",
            "message": f"SQL connection failed: {str(e)}",
            "detail": traceback.format_exc(),
        })


@app.get("/test-ocr")
async def test_ocr():
    try:
        import pytesseract
        version = pytesseract.get_tesseract_version()
        return {"status": "ok", "tesseract_version": str(version)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})


# ------------------------------------------------------------------ #
# UC Volume file listing                                               #
# ------------------------------------------------------------------ #

@app.get("/volume-files")
async def list_volume_files():
    """List all PDF files available in the UC Volume."""
    try:
        if not os.path.isdir(UC_VOLUME_PATH):
            return JSONResponse(status_code=500, content={
                "error": f"UC Volume path not accessible: {UC_VOLUME_PATH}"
            })
        files = [
            {
                "name": f.name,
                "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
                "path": str(f),
            }
            for f in Path(UC_VOLUME_PATH).iterdir()
            if f.is_file() and f.suffix.lower() == ".pdf"
        ]
        files.sort(key=lambda x: x["name"])
        logger.info(f"Listed {len(files)} PDF files in Volume")
        return {"files": files, "volume_path": UC_VOLUME_PATH}
    except Exception as e:
        logger.error(f"Error listing volume files: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": f"Could not list volume files: {str(e)}"})


# ------------------------------------------------------------------ #
# Auto-detect columns from first page                                  #
# ------------------------------------------------------------------ #

@app.post("/autoparse_columns")
async def autoparse_columns(file: UploadFile = File(...)):
    upload_dir, _ = ensure_temp_dirs()
    temp_filename = None
    try:
        if not file.filename.lower().endswith(".pdf"):
            return JSONResponse(status_code=400, content={"error": "Only PDF files are supported"})

        temp_filename = os.path.join(upload_dir, f"{uuid.uuid4()}_{file.filename}")
        with open(temp_filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        logger.info(f"Analysing PDF for column suggestions: {file.filename}")
        images = extractor.pdf_to_images(temp_filename, dpi=200)
        if not images:
            return JSONResponse(status_code=500, content={"error": "Could not convert PDF to images"})

        base64_image = extractor.encode_image(images[0])
        prompt = (
            "Analyse this document image and identify the column headers for any tables present.\n\n"
            "Return ONLY a JSON array of column names, like this:\n"
            '["Column1", "Column2", "Column3"]\n\n'
            "If there are multiple tables, focus on the main data table. "
            "Extract the exact column names as they appear."
        )
        response_text = query_vision_model(base64_image, prompt)
        logger.info(f"Raw vision model response: {response_text}")

        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()

        if not response_text.startswith("["):
            start = response_text.find("[")
            end = response_text.rfind("]") + 1
            if start != -1 and end > start:
                response_text = response_text[start:end]

        columns = json.loads(response_text)
        if isinstance(columns, list):
            logger.info(f"Detected columns: {columns}")
            return {"columns": columns}

        return JSONResponse(status_code=500, content={"error": "Model returned unexpected format"})

    except requests.HTTPError as e:
        logger.error(f"Vision model HTTP error: {e.response.status_code} – {e.response.text}")
        return JSONResponse(status_code=502, content={
            "error": "Vision model request failed",
            "http_status": e.response.status_code,
            "detail": e.response.text,
        })
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
        return JSONResponse(status_code=500, content={"error": f"Could not parse column suggestions: {str(e)}"})
    except Exception as e:
        logger.error(f"Error in autoparse_columns:\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": f"Internal error: {str(e)}"})
    finally:
        if temp_filename and os.path.exists(temp_filename):
            try:
                os.remove(temp_filename)
            except Exception:
                pass


@app.get("/autoparse_columns_from_volume")
async def autoparse_columns_from_volume(filename: str):
    """Auto-detect columns from a file already in the UC Volume."""
    volume_file_path = os.path.join(UC_VOLUME_PATH, filename)
    if not os.path.exists(volume_file_path):
        return JSONResponse(status_code=404, content={"error": f"File not found in volume: {filename}"})

    try:
        images = extractor.pdf_to_images(volume_file_path, dpi=200)
        if not images:
            return JSONResponse(status_code=500, content={"error": "Could not convert PDF to images"})

        base64_image = extractor.encode_image(images[0])
        prompt = (
            "Analyse this document image and identify the column headers for any tables present.\n\n"
            "Return ONLY a JSON array of column names, like this:\n"
            '["Column1", "Column2", "Column3"]\n\n'
            "If there are multiple tables, focus on the main data table. "
            "Extract the exact column names as they appear."
        )
        response_text = query_vision_model(base64_image, prompt)

        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()

        if not response_text.startswith("["):
            start = response_text.find("[")
            end = response_text.rfind("]") + 1
            if start != -1 and end > start:
                response_text = response_text[start:end]

        columns = json.loads(response_text)
        if isinstance(columns, list):
            return {"columns": columns}

        return JSONResponse(status_code=500, content={"error": "Model returned unexpected format"})

    except Exception as e:
        logger.error(f"Error autoparsing volume file:\n{traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": f"Internal error: {str(e)}"})


# ------------------------------------------------------------------ #
# Shared extraction runner                                             #
# ------------------------------------------------------------------ #

def _run_extraction(
    pdf_path: str,
    columns_list: List[str],
    extra_instructions: str,
    mode: str,
    dpi: int,
    page_ranges: Optional[str],
    sample_pages: Optional[int],
) -> str:
    return extractor.process_pdf_enhanced(
        pdf_path=pdf_path,
        columns=columns_list,
        extra_instructions=extra_instructions,
        mode=mode,
        dpi=dpi,
        page_ranges=page_ranges,
        sample_pages=sample_pages,
    )


def _create_job(original_filename: str) -> str:
    job_id = str(uuid.uuid4())
    job_store[job_id] = {"status": "running", "filename": original_filename}
    return job_id


def _finish_job(job_id: str, output_path: str, original_filename: str):
    job_store[job_id] = {
        "status": "done",
        "output_path": output_path,
        "filename": original_filename.replace(".pdf", "_extracted.xlsx"),
    }


def _fail_job(job_id: str, error: str):
    job_store[job_id] = {"status": "error", "error": error}


# ------------------------------------------------------------------ #
# /extract — upload a PDF, queue extraction                           #
# ------------------------------------------------------------------ #

@app.post("/extract")
async def extract_table(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    columns: str = Form(...),
    extra_instructions: str = Form(""),
    mode: str = Form("scientific"),
    dpi: int = Form(200),
    page_ranges: Optional[str] = Form(None),
    sample_pages: Optional[int] = Form(None),
):
    upload_dir, _ = ensure_temp_dirs()
    logger.info(f"Extraction request – file: {file.filename}, columns: {columns}")

    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse(status_code=400, content={"error": "Only PDF files are supported"})

    try:
        columns_list: List[str] = json.loads(columns)
        if not isinstance(columns_list, list) or len(columns_list) == 0:
            raise ValueError("columns must be a non-empty array")
    except (json.JSONDecodeError, ValueError) as e:
        return JSONResponse(status_code=400, content={"error": f"Invalid columns parameter: {str(e)}"})

    job_id = _create_job(file.filename)
    temp_filename = os.path.join(upload_dir, f"{job_id}_{file.filename}")
    original_filename = file.filename

    with open(temp_filename, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    logger.info(f"Saved upload to: {temp_filename}")

    def run_job():
        logger.info(f"Background job {job_id} started")
        try:
            output_path = _run_extraction(
                temp_filename, columns_list, extra_instructions,
                mode, dpi, page_ranges, sample_pages,
            )
            _finish_job(job_id, output_path, original_filename)
            logger.info(f"Job {job_id} complete")
        except Exception as e:
            logger.error(f"Job {job_id} failed: {traceback.format_exc()}")
            _fail_job(job_id, str(e))
        finally:
            if os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                except Exception as ex:
                    logger.warning(f"Could not delete temp file: {ex}")

    background_tasks.add_task(run_job)
    logger.info(f"Job {job_id} queued for {original_filename}")
    return {"job_id": job_id, "status": "running"}


# ------------------------------------------------------------------ #
# /extract-from-volume — use a file already in UC Volume             #
# ------------------------------------------------------------------ #

@app.post("/extract-from-volume")
async def extract_from_volume(
    background_tasks: BackgroundTasks,
    filename: str = Form(...),
    columns: str = Form(...),
    extra_instructions: str = Form(""),
    mode: str = Form("scientific"),
    dpi: int = Form(200),
    page_ranges: Optional[str] = Form(None),
    sample_pages: Optional[int] = Form(None),
):
    volume_file_path = os.path.join(UC_VOLUME_PATH, filename)
    if not os.path.exists(volume_file_path):
        return JSONResponse(status_code=404, content={"error": f"File not found in volume: {filename}"})

    logger.info(f"Volume extraction request – file: {filename}, columns: {columns}")

    try:
        columns_list: List[str] = json.loads(columns)
        if not isinstance(columns_list, list) or len(columns_list) == 0:
            raise ValueError("columns must be a non-empty array")
    except (json.JSONDecodeError, ValueError) as e:
        return JSONResponse(status_code=400, content={"error": f"Invalid columns parameter: {str(e)}"})

    job_id = _create_job(filename)

    def run_job():
        logger.info(f"Volume job {job_id} started for {filename}")
        try:
            # Run directly from volume path — no staging needed since
            # _extract_with_ai_parse_document will stage it internally
            output_path = _run_extraction(
                volume_file_path, columns_list, extra_instructions,
                mode, dpi, page_ranges, sample_pages,
            )
            _finish_job(job_id, output_path, filename)
            logger.info(f"Volume job {job_id} complete")
        except Exception as e:
            logger.error(f"Volume job {job_id} failed: {traceback.format_exc()}")
            _fail_job(job_id, str(e))

    background_tasks.add_task(run_job)
    logger.info(f"Volume job {job_id} queued for {filename}")
    return {"job_id": job_id, "status": "running"}


# ------------------------------------------------------------------ #
# /status and /download                                                #
# ------------------------------------------------------------------ #

@app.get("/status/{job_id}")
async def job_status(job_id: str):
    job = job_store.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    if job["status"] == "error":
        return JSONResponse(status_code=500, content={"status": "error", "error": job["error"]})
    return {"status": job["status"], "job_id": job_id, "filename": job.get("filename", "")}


@app.get("/download/{job_id}")
async def download_result(job_id: str):
    job = job_store.get(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    if job["status"] != "done":
        return JSONResponse(status_code=202, content={"status": job["status"], "message": "Not ready yet"})
    if not os.path.exists(job["output_path"]):
        return JSONResponse(status_code=500, content={"error": "Output file missing"})
    return FileResponse(
        job["output_path"],
        filename=job["filename"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ------------------------------------------------------------------ #
# Frontend static file serving                                         #
# ------------------------------------------------------------------ #

if os.path.exists("frontend/build/static"):
    app.mount("/static", StaticFiles(directory="frontend/build/static"), name="static")
    logger.info("Mounted frontend static files")


@app.get("/")
async def read_index():
    index_path = "frontend/build/index.html"
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Frontend not built – API docs at /docs"}


@app.get("/{rest_of_path:path}")
async def react_app(rest_of_path: str):
    if any(
        rest_of_path.startswith(p)
        for p in (
            "api/", "docs", "openapi.json", "health", "extract",
            "autoparse_columns", "test-ocr", "test-sql", "test-vision",
            "status/", "download/", "volume-files",
        )
    ):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    index_path = "frontend/build/index.html"
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Frontend not built"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    logger.info(f"Starting server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, timeout_keep_alive=300)