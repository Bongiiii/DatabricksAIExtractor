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

job_store: Dict[str, Any] = {}

def ensure_temp_dirs():
    upload_dir = "/tmp/uploaded"
    extract_dir = "/tmp/extracted_tables"
    os.makedirs(upload_dir, exist_ok=True)
    os.makedirs(extract_dir, exist_ok=True)
    return upload_dir, extract_dir

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

# ------------------------------------------------------------------ #
# Helper for Iterative Parsing
# ------------------------------------------------------------------ #

async def get_columns_iteratively(pdf_path: str) -> List[str]:
    images = extractor.pdf_to_images(pdf_path, dpi=200)
    if not images:
        return []

    prompt = """
Task: Extract table column headers from this document image.

Expected Output:
["column_name_1", "column_name_2", "column_name_3"]

Rules:
- If no table is present, return []
- Output ONLY valid JSON
- No explanations or markdown
- Preserve exact column text
"""
    
    columns = []
    # scan first few pages until columns found
    for i, image in enumerate(images[:5]):
        base64_image = extractor.encode_image(image)
        response_text = query_vision_model(base64_image, prompt)

        logger.info(f"Page {i+1} raw vision response: {response_text}")

        cleaned = response_text.strip()
        if "```json" in cleaned:
            cleaned = cleaned.split("```json")[1].split("```")[0]
        elif "```" in cleaned:
            cleaned = cleaned.split("```")[1].split("```")[0]

        cleaned = cleaned.strip()
        if not cleaned.startswith("["):
            start = cleaned.find("[")
            end = cleaned.rfind("]") + 1
            if start != -1 and end > start:
                cleaned = cleaned[start:end]

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list) and len(parsed) > 0:
                logger.info(f"Detected columns on page {i+1}: {parsed}")
                return parsed
        except Exception as e:
            logger.warning(f"Page {i+1} parse failed: {e}")
            
    return []

# ------------------------------------------------------------------ #
# Endpoints
# ------------------------------------------------------------------ #

@app.get("/health")
async def health_check():
    # ... (Keep original health check logic)
    return {"status": "healthy"}

@app.get("/volume-files")
async def list_volume_files():
    try:
        if not os.path.isdir(UC_VOLUME_PATH):
            return JSONResponse(status_code=500, content={"error": "UC Volume path not accessible"})
        files = [{"name": f.name, "size_mb": round(f.stat().st_size / (1024 * 1024), 2), "path": str(f)}
                 for f in Path(UC_VOLUME_PATH).iterdir() if f.is_file() and f.suffix.lower() == ".pdf"]
        files.sort(key=lambda x: x["name"])
        return {"files": files, "volume_path": UC_VOLUME_PATH}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/autoparse_columns")
async def autoparse_columns(file: UploadFile = File(...)):
    upload_dir, _ = ensure_temp_dirs()
    temp_filename = os.path.join(upload_dir, f"{uuid.uuid4()}_{file.filename}")
    try:
        with open(temp_filename, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        columns = await get_columns_iteratively(temp_filename)
        return {"columns": columns}
    except Exception as e:
        logger.error(f"Error in autoparse_columns: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

@app.get("/autoparse_columns_from_volume")
async def autoparse_columns_from_volume(filename: str):
    volume_file_path = os.path.join(UC_VOLUME_PATH, filename)
    if not os.path.exists(volume_file_path):
        return JSONResponse(status_code=404, content={"error": "File not found"})
    try:
        columns = await get_columns_iteratively(volume_file_path)
        return {"columns": columns}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

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
    # ... (Keep original extraction logic)
    return {"job_id": "example", "status": "running"}

@app.get("/status/{job_id}")
async def job_status(job_id: str):
    job = job_store.get(job_id)
    if not job: return JSONResponse(status_code=404, content={"error": "Job not found"})
    return job

@app.get("/download/{job_id}")
async def download_result(job_id: str):
    job = job_store.get(job_id)
    if not job or job["status"] != "done": return JSONResponse(status_code=404, content={"error": "Not ready"})
    return FileResponse(job["output_path"], filename=job["filename"])

# ------------------------------------------------------------------ #
# Main Entry / Static Files (KEEP UNTOUCHED)
# ------------------------------------------------------------------ #

if os.path.exists("frontend/build/static"):
    app.mount("/static", StaticFiles(directory="frontend/build/static"), name="static")
    logger.info("Mounted frontend static files")

@app.get("/")
async def read_index():
    index_path = "frontend/build/index.html"
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Frontend not built - API docs at /docs"}

@app.get("/{rest_of_path:path}")
async def react_app(rest_of_path: str):
    if any(rest_of_path.startswith(p) for p in (
        "api/", "docs", "openapi.json", "health", "extract",
        "autoparse_columns", "test-ocr", "test-sql", "test-vision",
        "status/", "download/", "volume-files",
    )):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    index_path = "frontend/build/index.html"
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Frontend not built"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, timeout_keep_alive=300)
