import os
import io
import json
import base64
import shutil
import requests
from pathlib import Path
from typing import List, Dict, Any, Optional
import pandas as pd
from PIL import Image
import fitz  # PyMuPDF
import pytesseract
from pdf2image import convert_from_path
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ChatMessage, ChatMessageRole
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

UC_VOLUME_PATH = os.getenv("UC_VOLUME_PATH", "/Volumes/pdfs/default/sample-files")


class EnhancedPDFExtractor:
    def __init__(self):
        self.w = WorkspaceClient()
        self.output_dir = "/tmp/extracted_tables"
        os.makedirs(self.output_dir, exist_ok=True)
        logger.info(f"Output directory: {self.output_dir}")

        self.sql_http_path = os.getenv("DATABRICKS_SQL_HTTP_PATH")
        if not self.sql_http_path:
            logger.warning("DATABRICKS_SQL_HTTP_PATH not set – ai_parse_document disabled.")

        self.vision_model = os.getenv("VISION_MODEL_NAME", "databricks-gemma-3-12b")
        self.text_model = os.getenv("TEXT_MODEL_NAME", "databricks-gpt-oss-120b")

        logger.info(f"Vision model: {self.vision_model}")
        logger.info(f"Text model:   {self.text_model}")

    def _get_auth_headers(self) -> dict:
        auth_headers = self.w.config.authenticate()
        return {**auth_headers, "Content-Type": "application/json"}

    def _query_vision_model(self, base64_image: str, prompt: str) -> str:
        workspace_host = self.w.config.host.rstrip("/")
        endpoint_url = f"{workspace_host}/serving-endpoints/{self.vision_model}/invocations"

        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{base64_image}"},
                        },
                    ],
                }
            ],
            "max_tokens": 4096,
            "temperature": 0.05,
        }

        resp = requests.post(
            endpoint_url,
            headers=self._get_auth_headers(),
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def encode_image(self, image: Image.Image) -> str:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    def is_scanned_pdf(self, pdf_path: str) -> bool:
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(min(3, len(doc))):
                if len(doc.load_page(page_num).get_text().strip()) > 50:
                    doc.close()
                    return False
            doc.close()
            return True
        except Exception as e:
            logger.error(f"Error checking if PDF is scanned: {e}")
            return False

    def pdf_to_images(
        self,
        pdf_path: str,
        dpi: int = 200,
        page_ranges: Optional[List[int]] = None,
    ) -> List[Image.Image]:
        try:
            doc = fitz.open(pdf_path)
            images = []
            indices = page_ranges if page_ranges is not None else range(len(doc))
            for page_num in indices:
                page = doc.load_page(page_num)
                mat = fitz.Matrix(dpi / 72, dpi / 72)
                pix = page.get_pixmap(matrix=mat)
                images.append(Image.open(io.BytesIO(pix.tobytes("png"))))
            doc.close()
            return images
        except Exception as e:
            logger.error(f"Error converting PDF to images: {e}")
            raise

    def _get_pdf_page_count(self, pdf_path: str) -> int:
        try:
            doc = fitz.open(pdf_path)
            count = len(doc)
            doc.close()
            return count
        except Exception as e:
            logger.error(f"Error getting page count: {e}")
            return 0

    def _parse_page_ranges(self, page_ranges: str) -> List[int]:
        pages = []
        try:
            for part in page_ranges.split(","):
                part = part.strip()
                if "-" in part:
                    start, end = map(int, part.split("-"))
                    pages.extend(range(start - 1, end))
                else:
                    pages.append(int(part) - 1)
            return sorted(set(pages))
        except Exception as e:
            logger.error(f"Error parsing page ranges: {e}")
            return []

    def _generate_enhanced_column_definitions(self, columns: List[str]) -> str:
        definitions = []
        for col in columns:
            col_lower = col.lower()
            if any(w in col_lower for w in ["species", "scientific"]):
                definitions.append(f"- {col}: Scientific binomial names – Latin genus + species")
            elif any(w in col_lower for w in ["location", "distribution"]):
                definitions.append(f"- {col}: Geographic information – leave BLANK if not present")
            else:
                definitions.append(f"- {col}: Data for this column – leave BLANK if not present")
        return "\n".join(definitions)

    def _parse_partial_response(self, text: str, columns: List[str]) -> List[Dict]:
        return []

    def _normalize_response_text(self, content) -> str:
        """Handle models that return content as a list of blocks vs a plain string."""
        if isinstance(content, list):
            return " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        return content

    def ocr_pdf_with_tesseract(self, pdf_path: str, dpi: int = 300) -> str:
        try:
            logger.info(f"Starting Tesseract OCR on {pdf_path}...")
            images = convert_from_path(pdf_path, dpi=dpi)
            ocr_text = ""
            for i, image in enumerate(images):
                logger.info(f"OCR page {i + 1}/{len(images)}")
                ocr_text += f"\n--- Page {i + 1} ---\n"
                ocr_text += pytesseract.image_to_string(image, config="--psm 6")
            logger.info(f"OCR complete – {len(ocr_text)} characters extracted")
            return ocr_text
        except Exception as e:
            logger.error(f"Error during OCR: {e}")
            raise

    def _get_sql_connection(self):
        try:
            from databricks import sql as dbsql
        except ImportError:
            raise RuntimeError("databricks-sql-connector not installed.")

        if not self.sql_http_path:
            raise RuntimeError("DATABRICKS_SQL_HTTP_PATH not set.")

        host = self.w.config.host.rstrip("/").replace("https://", "")
        auth_headers = self.w.config.authenticate()
        token = auth_headers.get("Authorization", "").replace("Bearer ", "")

        if not token:
            raise RuntimeError("Could not resolve access token from workspace config.")

        return dbsql.connect(
            server_hostname=host,
            http_path=self.sql_http_path,
            access_token=token,
            _socket_timeout=1200,
        )

    def _stage_pdf_to_volume(self, pdf_path: str) -> str:
        """Copy the uploaded PDF into the UC Volume and return its Volume path."""
        filename = Path(pdf_path).name
        volume_dest = f"{UC_VOLUME_PATH}/{filename}"
        shutil.copy2(pdf_path, volume_dest)
        logger.info(f"Staged PDF to Volume: {volume_dest}")
        return volume_dest

    def _cleanup_volume_file(self, volume_path: str):
        try:
            os.remove(volume_path)
            logger.info(f"Removed staged file from Volume: {volume_path}")
        except Exception as e:
            logger.warning(f"Could not remove staged Volume file: {e}")

    def _extract_with_ai_parse_document(
        self, pdf_path: str, columns: List[str], extra_instructions: str = ""
    ) -> List[Dict[str, Any]]:

        file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
        if file_size_mb > 10:
            logger.warning(
                f"PDF too large ({file_size_mb:.1f}MB) for ai_parse_document – using vision fallback"
            )
            raise RuntimeError(
                f"PDF size {file_size_mb:.1f}MB exceeds limit for ai_parse_document"
            )

        # Stage to Volume so READ_FILES can access it
        volume_path = self._stage_pdf_to_volume(pdf_path)

        conn = self._get_sql_connection()
        try:
            with conn.cursor() as cursor:

                sql_parse = f"""
                    CREATE OR REPLACE TEMPORARY VIEW parsed_pdf AS
                    SELECT
                        path,
                        ai_parse_document(
                            content,
                            MAP(
                                'version', '2.0',
                                'imageOutputPath', '{UC_VOLUME_PATH}/',
                                'descriptionElementTypes', '*'
                            )
                        ) AS parsed
                    FROM READ_FILES(
                        '{volume_path}',
                        format => 'binaryFile'
                    )
                """
                logger.info("Running ai_parse_document via READ_FILES...")
                cursor.execute(sql_parse)

                sql_all_elements = """
                    CREATE OR REPLACE TEMPORARY VIEW all_elements AS
                    WITH valid_docs AS (
                        SELECT parsed
                        FROM parsed_pdf
                        WHERE try_cast(parsed:error_status AS STRING) IS NULL
                    ),
                    exploded AS (
                        SELECT
                            posexplode(
                                try_cast(parsed:document:elements AS ARRAY<VARIANT>)
                            ) AS (element_pos, elem)
                        FROM valid_docs
                    )
                    SELECT
                        element_pos,
                        try_cast(elem:type        AS STRING) AS element_type,
                        try_cast(elem:page        AS INT)    AS page_num,
                        try_cast(elem:content     AS STRING) AS content,
                        try_cast(elem:description AS STRING) AS description
                    FROM exploded
                    ORDER BY page_num, element_pos
                """
                logger.info("Creating all_elements temp view...")
                cursor.execute(sql_all_elements)

                sql_pdf_elements = """
                    CREATE OR REPLACE TEMPORARY VIEW pdf_elements AS
                    SELECT
                        element_type,
                        page_num,
                        LEFT(content, 200)     AS content_preview,
                        LEFT(description, 200) AS description_preview
                    FROM all_elements
                """
                logger.info("Creating pdf_elements preview view...")
                cursor.execute(sql_pdf_elements)

                cursor.execute("""
                    SELECT element_type, COUNT(*) as cnt
                    FROM pdf_elements
                    GROUP BY element_type
                    ORDER BY cnt DESC
                """)
                for row in cursor.fetchall():
                    logger.info(f"Element type '{row[0]}': {row[1]} elements")

                cursor.execute("""
                    SELECT page_num, content_preview, description_preview
                    FROM pdf_elements
                    WHERE element_type = 'figure'
                    ORDER BY page_num
                """)
                figures = cursor.fetchall()
                logger.info(f"Found {len(figures)} figure elements")

                cursor.execute("""
                    SELECT
                        element_type,
                        page_num,
                        element_pos,
                        content,
                        description
                    FROM all_elements
                    WHERE element_type IN (
                        'title', 'section_header', 'text', 'table', 'caption', 'figure'
                    )
                    ORDER BY page_num, element_pos
                """)
                rows = cursor.fetchall()

        finally:
            conn.close()
            self._cleanup_volume_file(volume_path)

        if not rows:
            logger.warning(
                "ai_parse_document returned no rows – possible parse error or empty document"
            )
            return []

        pages: Dict[int, List[str]] = {}
        for element_type, page_num, element_pos, content, description in rows:
            if page_num is None:
                logger.warning(
                    f"Element {element_pos} ({element_type}) has no page number – assigned to page 1"
                )
                pn = 1
            else:
                pn = page_num
            pages.setdefault(pn, [])
            if element_type == "figure" and description:
                pages[pn].append(f"[figure] {description}")
            elif content:
                line = f"[{element_type}] {content}"
                if len(line) > 5000:
                    logger.warning(f"Page {pn}: element truncated from {len(line)} chars")
                    line = line[:5000] + "... [truncated]"
                pages[pn].append(line)

        layout_lines = []
        for pn in sorted(pages.keys()):
            layout_lines.append(f"\n{'='*80}\nPAGE {pn}\n{'='*80}")
            layout_lines.extend(pages[pn])

        layout_text = "\n".join(layout_lines)
        logger.info(
            f"ai_parse_document: {len(rows)} elements across {len(pages)} pages – "
            f"layout length: {len(layout_text)} chars"
        )

        return self._extract_columns_from_layout(layout_text, columns, extra_instructions)

    def _extract_columns_from_layout(
        self, layout_text: str, columns: List[str], extra_instructions: str = ""
    ) -> List[Dict[str, Any]]:
        columns_str = '", "'.join(columns)
        column_placeholders = ", ".join([f'"{col}": "value or empty"' for col in columns])

        prompt = f"""
You are a data extraction assistant. Below is the structured layout of a document
produced by an automated parser. Tables may be represented as HTML.

Extract ALL rows of tabular data matching these columns: "{columns_str}"

DOCUMENT LAYOUT:
{layout_text[:60000]}

RULES:
1. Extract EVERY matching row.
2. Use "" for missing cells.
3. Preserve exact values.
4. Keep full scientific binomial names.
5. Infer implicit table structure from alignment.
6. Ignore headers, page numbers, section titles.

{extra_instructions}

Return ONLY a valid JSON array – no markdown:
[
  {{{column_placeholders}}},
  ...
]
"""
        try:
            response = self.w.serving_endpoints.query(
                name=self.text_model,
                messages=[ChatMessage(role=ChatMessageRole.USER, content=prompt)],
                max_tokens=16000,
                temperature=0.05,
            )
            response_text = self._normalize_response_text(
                response.choices[0].message.content
            )
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]
            response_text = response_text.strip()

            result = json.loads(response_text)
            if isinstance(result, list):
                logger.info(f"text_model extracted {len(result)} rows")
                return result
            if isinstance(result, dict):
                return result.get("extracted_data", [])
            return []
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            return []
        except Exception as e:
            logger.error(f"Error querying text_model: {e}")
            return []

    def _extract_from_ocr_text_with_llm(
        self, text: str, columns: List[str], extra_instructions: str = ""
    ) -> List[Dict[str, Any]]:
        columns_str = '", "'.join(columns)
        column_placeholders = ", ".join([f'"{col}": "value"' for col in columns])

        prompt = f"""
You are analysing OCR-extracted text from a scanned document.
Extract ALL tabular data matching these columns: "{columns_str}"

OCR TEXT:
{text[:15000]}

RULES:
1. Extract every matching data row.
2. Use "" for missing cells.
3. Remove OCR artefacts.
4. Infer implicit table structure from spacing.
5. Maintain row alignment.

{extra_instructions}

Return ONLY a valid JSON array:
[
  {{{column_placeholders}}},
  ...
]
"""
        try:
            response = self.w.serving_endpoints.query(
                name=self.text_model,
                messages=[ChatMessage(role=ChatMessageRole.USER, content=prompt)],
                max_tokens=16000,
                temperature=0.05,
            )
            response_text = self._normalize_response_text(
                response.choices[0].message.content
            )
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]
            response_text = response_text.strip()

            result = json.loads(response_text)
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result.get("extracted_data", [])
            return []
        except Exception as e:
            logger.error(f"Error extracting from OCR text: {e}")
            return [] 
        
    def extract_dense_table_data(
        self,
        image: Image.Image,
        columns: List[str],
        extra_instructions: str = "",
        page_num: int = 0,
    ) -> List[Dict[str, Any]]:
        try:
            base64_image = self.encode_image(image)
            columns_str = '", "'.join(columns)
            column_placeholders = ", ".join(
                [f'"{col}": "extracted_value_or_empty_string"' for col in columns]
            )

            prompt = f"""
Extract ALL tabular data from this page matching these columns: "{columns_str}"

RULES:
1. Scan entire page top-to-bottom.
2. Tables may be implicit – infer from alignment.
3. Leave cells BLANK ("") when no data visible.
4. Ignore headers and page numbers.

COLUMN DEFINITIONS:
{self._generate_enhanced_column_definitions(columns)}

CONTEXT: {extra_instructions or "None."}

Return ONLY a valid JSON object:
{{
  "extracted_data": [
    {{{column_placeholders}}}
  ],
  "total_rows": <integer>
}}
"""
            response_text = self._query_vision_model(base64_image, prompt)
            logger.info(
                f"Page {page_num + 1} raw vision response (first 200 chars): {response_text[:200]}"
            )

            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]
            response_text = response_text.strip()

            try:
                result = json.loads(response_text)
                rows = result.get("extracted_data", [])
                logger.info(f"Page {page_num + 1}: extracted {len(rows)} rows")
                return rows
            except json.JSONDecodeError:
                return self._parse_partial_response(response_text, columns)

        except requests.HTTPError as e:
            logger.error(
                f"Vision model HTTP error on page {page_num + 1}: "
                f"{e.response.status_code} – {e.response.text}"
            )
            return []
        except Exception as e:
            logger.error(f"Error processing page {page_num + 1}: {e}")
            return []

    def _fallback_vision_extraction(
        self,
        pdf_path: str,
        columns: List[str],
        extra_instructions: str = "",
        dpi: int = 200,
        page_ranges: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        logger.info("Using vision-based page-by-page fallback extraction...")
        images = self.pdf_to_images(pdf_path, dpi=dpi, page_ranges=page_ranges)
        all_data = []
        for page_num, image in enumerate(images):
            page_data = self.extract_dense_table_data(
                image, columns, extra_instructions, page_num
            )
            for row in page_data:
                row["_page_number"] = page_num + 1
            all_data.extend(page_data)
        return all_data

    def extract_with_best_method(
        self,
        pdf_path: str,
        columns: List[str],
        extra_instructions: str = "",
        dpi: int = 200,
        page_ranges: Optional[List[int]] = None,
    ) -> List[Dict[str, Any]]:
        is_scanned = self.is_scanned_pdf(pdf_path)
        logger.info(f"PDF type: {'scanned' if is_scanned else 'native/text'}")

        # Path A: Scanned → Tesseract → text_model
        if is_scanned:
            try:
                logger.info("Scanned PDF – running Tesseract OCR...")
                ocr_text = self.ocr_pdf_with_tesseract(pdf_path)
                result = self._extract_from_ocr_text_with_llm(
                    ocr_text, columns, extra_instructions
                )
                if result:
                    logger.info(f"OCR+LLM returned {len(result)} rows")
                    return result
                logger.warning("OCR+LLM returned no data – falling back to vision")
            except Exception as e:
                logger.error(f"OCR path failed: {e} – falling back to vision")

        # Path B: Native → READ_FILES → ai_parse_document → text_model
        else:
            if self.sql_http_path:
                try:
                    result = self._extract_with_ai_parse_document(
                        pdf_path, columns, extra_instructions
                    )
                    if result:
                        logger.info(f"ai_parse_document returned {len(result)} rows")
                        return result
                    logger.warning(
                        "ai_parse_document returned no data – falling back to vision"
                    )
                except Exception as e:
                    logger.error(f"ai_parse_document failed: {e} – falling back to vision")
            else:
                logger.warning("DATABRICKS_SQL_HTTP_PATH not set – using vision fallback")

        # Path C: vision model page-by-page
        return self._fallback_vision_extraction(
            pdf_path, columns, extra_instructions, dpi, page_ranges
        )

    def process_pdf_enhanced(
        self,
        pdf_path: str,
        columns: List[str],
        extra_instructions: str = "",
        mode: str = "scientific",
        dpi: int = 200,
        page_ranges: Optional[str] = None,
        sample_pages: Optional[int] = None,
    ) -> str:
        pdf_name = Path(pdf_path).stem
        output_excel_path = os.path.join(self.output_dir, f"{pdf_name}_extracted.xlsx")

        parsed_page_ranges: Optional[List[int]] = None
        if sample_pages and sample_pages > 0:
            parsed_page_ranges = list(
                range(min(sample_pages, self._get_pdf_page_count(pdf_path)))
            )
        elif page_ranges:
            parsed_page_ranges = self._parse_page_ranges(page_ranges)

        all_data = self.extract_with_best_method(
            pdf_path=pdf_path,
            columns=columns,
            extra_instructions=extra_instructions,
            dpi=dpi,
            page_ranges=parsed_page_ranges,
        )

        if all_data:
            self.create_excel_file(all_data, columns, output_excel_path)
            logger.info(f"Extraction complete – {len(all_data)} rows → {output_excel_path}")
        else:
            logger.warning("No data extracted from PDF")
            self.create_excel_file([], columns, output_excel_path)

        return output_excel_path


    def create_excel_file(self, all_data: List[Dict], columns: List[str], output_path: str):
        df = pd.DataFrame(all_data)
        for col in columns:
            if col not in df.columns:
                df[col] = ""
        ordered = [c for c in columns if c in df.columns]
        if "_page_number" in df.columns:
            ordered.append("_page_number")
        df[ordered].to_excel(output_path, index=False, engine="openpyxl")
        logger.info(f"Excel file created: {output_path}")
