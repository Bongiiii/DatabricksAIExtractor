import React, { useState, useRef, useEffect } from "react";
import axios from "axios";
import * as XLSX from "xlsx";

function App() {
  // Source mode: 'upload' or 'volume'
  const [sourceMode, setSourceMode] = useState("upload");

  // Upload mode state
  const [pdfFile, setPdfFile] = useState(null);
  const [originalFilename, setOriginalFilename] = useState("");

  // Volume mode state
  const [volumeFiles, setVolumeFiles] = useState([]);
  const [selectedVolumeFile, setSelectedVolumeFile] = useState("");
  const [isLoadingVolume, setIsLoadingVolume] = useState(false);

  // Shared state
  const [columns, setColumns] = useState("Species,Common Name,Location,Status");
  const [notes, setNotes] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [loadingStatus, setLoadingStatus] = useState("");
  const [previewData, setPreviewData] = useState([]);
  const [extractedBlob, setExtractedBlob] = useState(null);
  const [downloadLink, setDownloadLink] = useState("");
  const [extractionStats, setExtractionStats] = useState(null);
  const [error, setError] = useState("");
  const [samplePages, setSamplePages] = useState("");
  const [columnMode, setColumnMode] = useState(null);
  const [autoColumns, setAutoColumns] = useState([]);
  const [isAutoLoading, setIsAutoLoading] = useState(false);
  const [extractMode, setExtractMode] = useState("scientific");
  const [dpi, setDpi] = useState(200);
  const [pageRanges, setPageRanges] = useState("");

  const pollIntervalRef = useRef(null);
  const BACKEND_URL = window.location.origin;

  // Load volume files when switching to volume mode
  useEffect(() => {
    if (sourceMode === "volume") {
      fetchVolumeFiles();
    }
  }, [sourceMode]);

  const fetchVolumeFiles = async () => {
    setIsLoadingVolume(true);
    setError("");
    try {
      const res = await axios.get(`${BACKEND_URL}/volume-files`);
      setVolumeFiles(res.data.files || []);
      if (res.data.files.length === 0) {
        setError("No PDF files found in the UC Volume.");
      }
    } catch (err) {
      setError("Could not load volume files: " + (err?.response?.data?.error || err.message));
    }
    setIsLoadingVolume(false);
  };

  const resetResults = () => {
    setPreviewData([]);
    setExtractedBlob(null);
    setDownloadLink("");
    setExtractionStats(null);
    setError("");
    setLoadingStatus("");
  };

  const handleSourceModeChange = (mode) => {
    setSourceMode(mode);
    setColumnMode(null);
    setAutoColumns([]);
    resetResults();
  };

  const handleFileChange = (e) => {
    const file = e.target.files[0];
    setPdfFile(file);
    setOriginalFilename(file ? file.name : "");
    setColumnMode(null);
    setAutoColumns([]);
    resetResults();
  };

  const handleVolumeFileChange = (filename) => {
    setSelectedVolumeFile(filename);
    setColumnMode(null);
    setAutoColumns([]);
    resetResults();
  };

  // Auto-parse columns from uploaded file
  const handleAutoParseColumns = async () => {
    if (sourceMode === "upload" && !pdfFile) {
      setError("Please select a PDF file first"); return;
    }
    if (sourceMode === "volume" && !selectedVolumeFile) {
      setError("Please select a file from the volume first"); return;
    }

    setIsAutoLoading(true);
    setError("");

    try {
      if (sourceMode === "upload") {
        const formData = new FormData();
        formData.append("file", pdfFile);
        const response = await axios.post(`${BACKEND_URL}/autoparse_columns`, formData, {
          timeout: 60000,
          headers: { "Content-Type": "multipart/form-data" },
        });
        if (response.data?.columns) {
          setAutoColumns(response.data.columns);
          setColumns(response.data.columns.join(", "));
          setColumnMode("auto");
        }
      } else {
        const response = await axios.get(`${BACKEND_URL}/autoparse_columns_from_volume`, {
          params: { filename: selectedVolumeFile },
          timeout: 60000,
        });
        if (response.data?.columns) {
          setAutoColumns(response.data.columns);
          setColumns(response.data.columns.join(", "));
          setColumnMode("auto");
        }
      }
    } catch (err) {
      setError(err?.response?.data?.error || "Failed to autoparse columns. Try manual mode.");
    }
    setIsAutoLoading(false);
  };

  const validateInputs = () => {
    if (sourceMode === "upload" && !pdfFile) { setError("Please select a PDF file"); return false; }
    if (sourceMode === "volume" && !selectedVolumeFile) { setError("Please select a file from the volume"); return false; }
    if (!columns.trim()) { setError("Please specify at least one column"); return false; }
    const columnList = columns.split(",").map((c) => c.trim()).filter((c) => c);
    if (columnList.length === 0) { setError("Please specify valid column names"); return false; }
    if (samplePages && (isNaN(samplePages) || parseInt(samplePages) <= 0)) {
      setError("Sample pages must be a positive number"); return false;
    }
    return true;
  };

  // ------------------------------------------------------------------ //
  // Poll /status/{jobId} until done or error                             //
  // ------------------------------------------------------------------ //
  const startPolling = (jobId) => {
    setLoadingStatus("Extraction running — checking for results...");

    pollIntervalRef.current = setInterval(async () => {
      try {
        const res = await axios.get(`${BACKEND_URL}/status/${jobId}`);
        const { status } = res.data;

        if (status === "done") {
          clearInterval(pollIntervalRef.current);
          setLoadingStatus("Downloading results...");

          const downloadRes = await axios.get(`${BACKEND_URL}/download/${jobId}`, {
            responseType: "blob",
          });

          const blob = downloadRes.data;
          setExtractedBlob(blob);
          const link = URL.createObjectURL(blob);
          setDownloadLink(link);

          const reader = new FileReader();
          reader.onload = (e) => {
            try {
              const data = new Uint8Array(e.target.result);
              const workbook = XLSX.read(data, { type: "array" });
              const sheet = workbook.Sheets[workbook.SheetNames[0]];
              const json = XLSX.utils.sheet_to_json(sheet, { header: 1 });
              setPreviewData(json);
              setExtractionStats({
                totalRows: json.length > 0 ? json.length - 1 : 0,
                totalColumns: json.length > 0 ? json[0].length : 0,
                hasData: json.length > 1,
              });
            } catch (err) {
              setError("Could not parse extracted data for preview");
            }
          };
          reader.readAsArrayBuffer(blob);
          setIsLoading(false);
          setLoadingStatus("");

        } else if (status === "error") {
          clearInterval(pollIntervalRef.current);
          setError(`Extraction failed: ${res.data.error || "Unknown error"}`);
          setIsLoading(false);
          setLoadingStatus("");
        }
      } catch (err) {
        clearInterval(pollIntervalRef.current);
        setError(`Polling failed: ${err.message}`);
        setIsLoading(false);
        setLoadingStatus("");
      }
    }, 3000);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!validateInputs()) return;
    if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);

    setIsLoading(true);
    setLoadingStatus("Submitting request...");
    resetResults();

    const columnList = columns.split(",").map((c) => c.trim()).filter((c) => c);

    try {
      let response;

      if (sourceMode === "upload") {
        const formData = new FormData();
        formData.append("file", pdfFile);
        formData.append("columns", JSON.stringify(columnList));
        formData.append("extra_instructions", notes);
        formData.append("mode", extractMode);
        formData.append("dpi", dpi);
        if (pageRanges.trim()) formData.append("page_ranges", pageRanges.trim());
        if (samplePages && parseInt(samplePages) > 0) formData.append("sample_pages", parseInt(samplePages));

        response = await axios.post(`${BACKEND_URL}/extract`, formData, {
          timeout: 30000,
          headers: { "Content-Type": "multipart/form-data" },
        });

      } else {
        // Volume mode — send filename, no file upload needed
        const formData = new FormData();
        formData.append("filename", selectedVolumeFile);
        formData.append("columns", JSON.stringify(columnList));
        formData.append("extra_instructions", notes);
        formData.append("mode", extractMode);
        formData.append("dpi", dpi);
        if (pageRanges.trim()) formData.append("page_ranges", pageRanges.trim());
        if (samplePages && parseInt(samplePages) > 0) formData.append("sample_pages", parseInt(samplePages));

        response = await axios.post(`${BACKEND_URL}/extract-from-volume`, formData, {
          timeout: 30000,
          headers: { "Content-Type": "multipart/form-data" },
        });
      }

      const { job_id } = response.data;
      if (!job_id) {
        setError("Server did not return a job ID");
        setIsLoading(false);
        setLoadingStatus("");
        return;
      }

      setLoadingStatus("Job queued — extraction in progress...");
      startPolling(job_id);

    } catch (err) {
      console.error("Extraction request failed:", err);
      setError(err?.response?.data?.error || err.message || "Request failed");
      setIsLoading(false);
      setLoadingStatus("");
    }
  };

  const handleDownload = () => {
    if (!extractedBlob) return;
    const filename = sourceMode === "upload" ? originalFilename : selectedVolumeFile;
    const a = document.createElement("a");
    const url = URL.createObjectURL(extractedBlob);
    a.href = url;
    a.download = `extracted_${filename.replace(".pdf", ".xlsx")}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const handleDiscard = () => {
    if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    if (downloadLink) URL.revokeObjectURL(downloadLink);
    resetResults();
  };

  const renderPreviewTable = () => {
    if (previewData.length === 0) return null;
    const maxPreviewRows = 20;
    const previewRows = previewData.slice(0, maxPreviewRows);

    return (
      <div style={{ marginTop: "2rem", overflowX: "auto" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1rem" }}>
          <h4>Extraction Preview</h4>
          {extractionStats && (
            <div style={{ fontSize: "0.9em", color: "#666" }}>
              {extractionStats.totalRows} rows × {extractionStats.totalColumns} columns
              {previewData.length > maxPreviewRows && ` (showing first ${maxPreviewRows} rows)`}
            </div>
          )}
        </div>
        <table border="1" cellPadding="8" cellSpacing="0" style={{ borderCollapse: "collapse", width: "100%", fontSize: "0.9em", maxHeight: "400px", display: "block", overflowY: "auto" }}>
          <thead style={{ position: "sticky", top: 0, backgroundColor: "#f5f5f5" }}>
            {previewRows.length > 0 && (
              <tr>
                {previewRows[0].map((header, j) => (
                  <th key={j} style={{ padding: "8px", backgroundColor: "#e0e0e0", fontWeight: "bold", minWidth: "120px" }}>
                    {header}
                  </th>
                ))}
              </tr>
            )}
          </thead>
          <tbody style={{ display: "table", width: "100%" }}>
            {previewRows.slice(1).map((row, i) => (
              <tr key={i} style={{ backgroundColor: i % 2 === 0 ? "#f9f9f9" : "white" }}>
                {row.map((cell, j) => (
                  <td key={j} style={{ padding: "6px 8px", borderBottom: "1px solid #ddd", minWidth: "120px", wordBreak: "break-word" }}>
                    {cell || "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {previewData.length > maxPreviewRows && (
          <div style={{ textAlign: "center", padding: "1rem", fontStyle: "italic", color: "#666" }}>
            ... and {previewData.length - maxPreviewRows} more rows. Download the full file to see all data.
          </div>
        )}
      </div>
    );
  };

  return (
    <div style={{ padding: "2rem", fontFamily: "Arial, sans-serif", maxWidth: "1000px", margin: "auto", backgroundColor: "#fafafa", minHeight: "100vh" }}>
      <div style={{ backgroundColor: "white", padding: "2rem", borderRadius: "8px", boxShadow: "0 2px 4px rgba(0,0,0,0.1)" }}>
        <h2 style={{ color: "#333", marginBottom: "1.5rem" }}>🔍 PDF Table Extractor</h2>

        <form onSubmit={handleSubmit}>

          {/* --- Source Mode Toggle --- */}
          <div style={{ marginBottom: "1.5rem" }}>
            <label style={{ display: "block", marginBottom: "0.5rem", fontWeight: "bold" }}>
              PDF Source:
            </label>
            <div style={{ display: "flex", gap: "1rem" }}>
              <button
                type="button"
                onClick={() => handleSourceModeChange("upload")}
                style={{
                  padding: "0.75rem 1.5rem",
                  backgroundColor: sourceMode === "upload" ? "#007bff" : "#e9ecef",
                  color: sourceMode === "upload" ? "white" : "#333",
                  border: "none",
                  borderRadius: "4px",
                  fontSize: "1em",
                  cursor: "pointer",
                  fontWeight: sourceMode === "upload" ? "bold" : "normal",
                }}
              >
                Upload PDF
              </button>
              <button
                type="button"
                onClick={() => handleSourceModeChange("volume")}
                style={{
                  padding: "0.75rem 1.5rem",
                  backgroundColor: sourceMode === "volume" ? "#6f42c1" : "#e9ecef",
                  color: sourceMode === "volume" ? "white" : "#333",
                  border: "none",
                  borderRadius: "4px",
                  fontSize: "1em",
                  cursor: "pointer",
                  fontWeight: sourceMode === "volume" ? "bold" : "normal",
                }}
              >
                Extract from UC Volume
              </button>
            </div>
          </div>

          {/* --- Upload Mode --- */}
          {sourceMode === "upload" && (
            <div style={{ marginBottom: "1.5rem" }}>
              <label style={{ display: "block", marginBottom: "0.5rem", fontWeight: "bold" }}>Upload PDF:</label>
              <input
                type="file"
                accept="application/pdf"
                onChange={handleFileChange}
                style={{ padding: "0.5rem", border: "2px dashed #ccc", borderRadius: "4px", width: "100%", backgroundColor: "#f9f9f9" }}
              />
              {pdfFile && (
                <div style={{ marginTop: "0.5rem", fontSize: "0.9em", color: "#666" }}>
                  Selected: {pdfFile.name} ({(pdfFile.size / 1024 / 1024).toFixed(2)} MB)
                </div>
              )}
            </div>
          )}

          {/* --- Volume Mode --- */}
          {sourceMode === "volume" && (
            <div style={{ marginBottom: "1.5rem" }}>
              <label style={{ display: "block", marginBottom: "0.5rem", fontWeight: "bold" }}>
                Select file from UC Volume:
                <span style={{ fontSize: "0.8em", fontWeight: "normal", color: "#666", marginLeft: "0.5rem" }}>
                  /Volumes/pdfs/default/sample-files
                </span>
              </label>
              {isLoadingVolume ? (
                <div style={{ color: "#666", padding: "0.5rem" }}>Loading volume files...</div>
              ) : (
                <>
                  <select
                    value={selectedVolumeFile}
                    onChange={(e) => handleVolumeFileChange(e.target.value)}
                    style={{ width: "100%", padding: "0.75rem", border: "1px solid #ddd", borderRadius: "4px", fontSize: "1em" }}
                  >
                    <option value="">-- Select a PDF --</option>
                    {volumeFiles.map((f) => (
                      <option key={f.name} value={f.name}>
                        {f.name} ({f.size_mb} MB)
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={fetchVolumeFiles}
                    style={{ marginTop: "0.5rem", padding: "0.4rem 0.8rem", backgroundColor: "#6c757d", color: "white", border: "none", borderRadius: "4px", fontSize: "0.85em", cursor: "pointer" }}
                  >
                    Refresh file list
                  </button>
                </>
              )}
            </div>
          )}

          {/* --- Column Mode Selection --- */}
          {((sourceMode === "upload" && pdfFile) || (sourceMode === "volume" && selectedVolumeFile)) && !columnMode && (
            <div style={{ marginBottom: "1.5rem" }}>
              <label style={{ fontWeight: "bold", marginBottom: "0.5rem", display: "block" }}>
                How would you like to select columns?
              </label>
              <button type="button" onClick={handleAutoParseColumns} disabled={isAutoLoading}
                style={{ marginRight: "1rem", padding: "0.75rem 1.5rem", backgroundColor: isAutoLoading ? "#ccc" : "#007bff", color: "white", border: "none", borderRadius: "4px", fontSize: "1em", cursor: isAutoLoading ? "not-allowed" : "pointer" }}>
                {isAutoLoading ? "Autoparsing..." : "Autoparse columns with AI"}
              </button>
              <button type="button" onClick={() => setColumnMode("manual")}
                style={{ padding: "0.75rem 1.5rem", backgroundColor: "#28a745", color: "white", border: "none", borderRadius: "4px", fontSize: "1em", cursor: "pointer" }}>
                Manually add columns
              </button>
            </div>
          )}

          {/* --- Columns Input --- */}
          {(columnMode === "manual" || columnMode === "auto") && (
            <div style={{ marginBottom: "1.5rem" }}>
              <label style={{ display: "block", marginBottom: "0.5rem", fontWeight: "bold" }}>Columns (comma-separated):</label>
              <input
                type="text"
                value={columns}
                onChange={(e) => setColumns(e.target.value)}
                style={{ width: "100%", padding: "0.75rem", border: "1px solid #ddd", borderRadius: "4px", fontSize: "1em" }}
                placeholder="e.g., Species, Common Name, Location, Status"
              />
              {columnMode === "auto" && autoColumns.length > 0 && (
                <div style={{ fontSize: "0.8em", color: "#666", marginTop: "0.25rem" }}>
                  <strong>Suggested:</strong> {autoColumns.join(", ")} — edit freely before extracting.
                </div>
              )}
              {columnMode === "manual" && (
                <div style={{ fontSize: "0.8em", color: "#666", marginTop: "0.25rem" }}>Example: Species, Common Name, Location, Status</div>
              )}
            </div>
          )}

          {/* --- Extraction Mode --- */}
          <div style={{ marginBottom: "1.5rem" }}>
            <label style={{ display: "block", marginBottom: "0.5rem", fontWeight: "bold" }}>Extraction Mode:</label>
            <select value={extractMode} onChange={e => setExtractMode(e.target.value)}
              style={{ padding: "0.75rem", borderRadius: "4px", border: "1px solid #ddd", fontSize: "1em" }}>
              <option value="scientific">Scientific Table Extraction</option>
              <option value="generic">Generic Table Extraction</option>
            </select>
            <div style={{ fontSize: "0.8em", color: "#666", marginTop: "0.25rem" }}>
              Choose "Generic" for non-scientific tables or mixed data.
            </div>
          </div>

          {/* --- DPI --- */}
          <div style={{ marginBottom: "1.5rem" }}>
            <label style={{ display: "block", marginBottom: "0.5rem", fontWeight: "bold" }}>Magnification (DPI):</label>
            <select value={dpi} onChange={e => setDpi(Number(e.target.value))}
              style={{ padding: "0.75rem", borderRadius: "4px", border: "1px solid #ddd", fontSize: "1em" }}>
              <option value={100}>100</option>
              <option value={200}>200 (default)</option>
              <option value={300}>300</option>
              <option value={400}>400</option>
            </select>
            <div style={{ fontSize: "0.8em", color: "#666", marginTop: "0.25rem" }}>
              Higher DPI can improve extraction for dense or small text tables.
            </div>
          </div>

          {/* --- Page Ranges --- */}
          <div style={{ marginBottom: "1.5rem" }}>
            <label style={{ display: "block", marginBottom: "0.5rem", fontWeight: "bold" }}>Page Ranges (e.g., 1,2,5-7):</label>
            <input type="text" value={pageRanges} onChange={e => setPageRanges(e.target.value)}
              style={{ width: "300px", padding: "0.75rem", border: "1px solid #ddd", borderRadius: "4px", fontSize: "1em" }}
              placeholder="e.g., 1,2,5-7" />
            <div style={{ fontSize: "0.8em", color: "#666", marginTop: "0.25rem" }}>
              Specify which pages to extract. Leave empty to process all pages.
            </div>
          </div>

          {/* --- Sample Pages --- */}
          <div style={{ marginBottom: "1.5rem" }}>
            <label style={{ display: "block", marginBottom: "0.5rem", fontWeight: "bold" }}>Test Mode - Sample Pages (optional):</label>
            <input type="number" value={samplePages} onChange={(e) => setSamplePages(e.target.value)}
              style={{ width: "200px", padding: "0.75rem", border: "1px solid #ddd", borderRadius: "4px", fontSize: "1em" }}
              placeholder="e.g., 3" min="1" />
            <div style={{ fontSize: "0.8em", color: "#666", marginTop: "0.25rem" }}>
              Leave empty to process all pages. Enter a number to test with first N pages only.
            </div>
          </div>

          {/* --- Extra Instructions --- */}
          <div style={{ marginBottom: "1.5rem" }}>
            <label style={{ display: "block", marginBottom: "0.5rem", fontWeight: "bold" }}>Extra Instructions (optional):</label>
            <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows="4"
              style={{ width: "100%", padding: "0.75rem", border: "1px solid #ddd", borderRadius: "4px", fontSize: "1rem", resize: "vertical" }}
              placeholder="Any specific instructions for data extraction..." />
          </div>

          {/* --- Error --- */}
          {error && (
            <div style={{ marginBottom: "1rem", padding: "1rem", backgroundColor: "#fee", border: "1px solid #fcc", borderRadius: "4px", color: "#c00" }}>
              {error}
            </div>
          )}

          <button type="submit" disabled={isLoading}
            style={{ padding: "1rem 2rem", backgroundColor: isLoading ? "#ccc" : sourceMode === "volume" ? "#6f42c1" : "#007bff", color: "white", border: "none", borderRadius: "4px", fontSize: "1.1em", cursor: isLoading ? "not-allowed" : "pointer", transition: "background-color 0.2s" }}>
            {isLoading ? "⏳ Extracting..." : sourceMode === "volume" ? "Extract from Volume" : "Extract Table"}
          </button>
        </form>

        {/* --- Loading State --- */}
        {isLoading && (
          <div style={{ marginTop: "2rem", padding: "1rem", backgroundColor: "#e3f2fd", border: "1px solid #2196f3", borderRadius: "4px", textAlign: "center" }}>
            <div>⏳ {loadingStatus || "Processing..."}</div>
            <div style={{ fontSize: "0.9em", color: "#666", marginTop: "0.5rem" }}>
              Checking for results every 3 seconds. Please don't close this window.
            </div>
          </div>
        )}

        {/* --- Success State --- */}
        {extractionStats && !isLoading && (
          <div style={{ marginTop: "2rem", padding: "1rem", backgroundColor: "#e8f5e8", border: "1px solid #4caf50", borderRadius: "4px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <div>
                <strong> Extraction Complete!</strong>
                <div style={{ fontSize: "0.9em", color: "#666", marginTop: "0.25rem" }}>
                  Found {extractionStats.totalRows} rows of data
                </div>
              </div>
              <div>
                <button onClick={handleDownload}
                  style={{ padding: "0.75rem 1.5rem", backgroundColor: "#28a745", color: "white", border: "none", borderRadius: "4px", marginRight: "0.5rem", cursor: "pointer", fontSize: "1em" }}>
                  Download Excel
                </button>
                <button onClick={handleDiscard}
                  style={{ padding: "0.75rem 1.5rem", backgroundColor: "#dc3545", color: "white", border: "none", borderRadius: "4px", cursor: "pointer", fontSize: "1em" }}>
                  Discard
                </button>
              </div>
            </div>
          </div>
        )}

        {renderPreviewTable()}
      </div>
    </div>
  );
}

export default App;