import React, { useState, useEffect } from "react";
import { type ProposedHunkItem } from "./DiffExtension";
import { SanitizedMarkdown } from "./FloatingChatBar";
import { useEnabledModels } from "../context/EnabledModelsContext";

export interface SelectionQuickActionBarProps {
  projectId: string;
  filePath: string;
  selectedText: string;
  startLine: number;
  endLine: number;
  csrfToken: string;
  model?: string;
  onHunkProposed?: (hunk: ProposedHunkItem) => void;
  onClose?: () => void;
}

export function SelectionQuickActionBar({
  projectId,
  filePath,
  selectedText,
  startLine,
  endLine,
  csrfToken,
  model: modelProp,
  onHunkProposed,
  onClose,
}: SelectionQuickActionBarProps): React.ReactElement {
  const { models: enabledModels } = useEnabledModels();
  const model = modelProp ?? enabledModels[0]?.id;
  const noModelTitle = model ? undefined : "Enable a model in Settings > Models";
  const [loadingAction, setLoadingAction] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [modalResult, setModalResult] = useState<{
    title: string;
    content: string;
    claimEvidence?: any[];
  } | null>(null);

  const handleAction = async (action: "tighten" | "verify_claim" | "formal_tone" | "explain_math") => {
    if (!model) return;
    setLoadingAction(action);
    setErrorMsg(null);

    try {
      const res = await fetch(`/api/projects/${projectId}/tools/quick-action`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          file_path: filePath,
          start_line: startLine,
          end_line: endLine,
          selected_text: selectedText,
          action,
          model,
        }),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `Action ${action} failed with status ${res.status}`);
      }

      const data = await res.json();
      if (data.hunk && onHunkProposed) {
        onHunkProposed(data.hunk);
      } else if (action === "explain_math") {
        setModalResult({
          title: "Math Explanation",
          content: data.explanation || "No explanation provided.",
        });
      } else if (action === "verify_claim") {
        setModalResult({
          title: "Claim Verification Evidence",
          content: "",
          claimEvidence: data.claim_evidence || [],
        });
      }
    } catch (err: any) {
      setErrorMsg(err.message || String(err));
    } finally {
      setLoadingAction(null);
    }
  };

  return (
    <div className="lemma-selection-toolbar" role="toolbar" aria-label="Academic Selection Actions">
      <div className="lemma-selection-buttons">
        <button
          type="button"
          className="lemma-btn lemma-btn-secondary lemma-btn-xs"
          aria-label="Tighten for Word/Page Limit"
          disabled={loadingAction !== null || !model}
          title={noModelTitle}
          onClick={() => handleAction("tighten")}
        >
          {loadingAction === "tighten" ? "Tightening..." : "Tighten"}
        </button>
        <button
          type="button"
          className="lemma-btn lemma-btn-secondary lemma-btn-xs"
          aria-label="Verify Claim Against Cited PDF"
          disabled={loadingAction !== null || !model}
          title={noModelTitle}
          onClick={() => handleAction("verify_claim")}
        >
          {loadingAction === "verify_claim" ? "Verifying..." : "Verify Claim"}
        </button>
        <button
          type="button"
          className="lemma-btn lemma-btn-secondary lemma-btn-xs"
          aria-label="Rewrite in Formal Academic Tone"
          disabled={loadingAction !== null || !model}
          title={noModelTitle}
          onClick={() => handleAction("formal_tone")}
        >
          {loadingAction === "formal_tone" ? "Rewriting..." : "Formal Tone"}
        </button>
        <button
          type="button"
          className="lemma-btn lemma-btn-secondary lemma-btn-xs"
          aria-label="Explain Math Step-by-Step"
          disabled={loadingAction !== null || !model}
          title={noModelTitle}
          onClick={() => handleAction("explain_math")}
        >
          {loadingAction === "explain_math" ? "Explaining..." : "Explain Math"}
        </button>
        {onClose && (
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary lemma-btn-xs lemma-toolbar-close-btn"
            aria-label="Close Toolbar"
            onClick={onClose}
          >
            ×
          </button>
        )}
      </div>

      {errorMsg && <p className="lemma-toolbar-error" role="alert">{errorMsg}</p>}

      {modalResult && (
        <div className="lemma-toolbar-result-popover" role="dialog" aria-label={modalResult.title}>
          <h3>{modalResult.title}</h3>
          {modalResult.content && <SanitizedMarkdown content={modalResult.content} />}
          {modalResult.claimEvidence && modalResult.claimEvidence.length > 0 && (
            <div className="lemma-claim-evidence-list">
              {modalResult.claimEvidence.map((ev, i) => (
                <div key={i} className="lemma-claim-evidence-item">
                  <strong>Cite: {ev.cite_key}</strong> (Page {ev.page_number ?? "N/A"})
                  <SanitizedMarkdown content={ev.snippet || ""} />
                </div>
              ))}
            </div>
          )}
          {modalResult.claimEvidence && modalResult.claimEvidence.length === 0 && (
            <p>No supporting PDF or abstract citations found for this claim.</p>
          )}
          <button type="button" onClick={() => setModalResult(null)}>Close</button>
        </div>
      )}
    </div>
  );
}

export interface HealthCheckModalProps {
  projectId: string;
  csrfToken: string;
  isOpen: boolean;
  onClose: () => void;
  onJumpToLine?: (filePath: string, line: number) => void;
  rootTex?: string;
}

export function HealthCheckModal({
  projectId,
  csrfToken,
  isOpen,
  onClose,
  onJumpToLine,
  rootTex = "main.tex",
}: HealthCheckModalProps): React.ReactElement | null {
  const [loading, setLoading] = useState<boolean>(false);
  const [report, setReport] = useState<any | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const runHealthCheck = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await fetch(`/api/projects/${projectId}/tools/health-check`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ root_tex: rootTex }),
      });
      if (!res.ok) throw new Error("Health check failed");
      const data = await res.json();
      setReport(data);
    } catch (err: any) {
      setErrorMsg(err.message || String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      runHealthCheck();
    }
  }, [isOpen, projectId]);

  if (!isOpen) return null;

  return (
    <div className="lemma-modal-backdrop" role="dialog" aria-label="Citation & Grounding Health Check">
      <div className="lemma-modal-card">
        <h2>Citation &amp; Grounding Health Check</h2>
        <button type="button" aria-label="Close" className="lemma-modal-close" onClick={onClose}>
          ×
        </button>

        {loading && <p>Auditing manuscript citations and empirical claims...</p>}
        {errorMsg && <p className="lemma-error" role="alert">{errorMsg}</p>}

        {report && (
          <div className="lemma-health-report">
            <section>
              <h3>Missing BibTeX Keys ({report.missing_bib_keys?.length || 0})</h3>
              {report.missing_bib_keys?.length === 0 ? (
                <p className="lemma-green">No missing citation keys found.</p>
              ) : (
                <ul>
                  {report.missing_bib_keys?.map((item: any, i: number) => (
                    <li
                      key={i}
                      className="lemma-clickable-issue"
                      onClick={() => onJumpToLine && onJumpToLine(item.file, item.line)}
                    >
                      <code>\cite&#123;{item.key}&#125;</code> in {item.file}:{item.line}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h3>Uncited BibTeX Entries ({report.uncited_bib_keys?.length || 0})</h3>
              {report.uncited_bib_keys?.length === 0 ? (
                <p className="lemma-green">All references.bib entries cited or \nocite&#123;*&#125; used.</p>
              ) : (
                <ul>
                  {report.uncited_bib_keys?.map((key: string, i: number) => (
                    <li key={i}><code>{key}</code></li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h3>Uncited Empirical / Quantitative Claims ({report.uncited_factual_claims?.length || 0})</h3>
              {report.uncited_factual_claims?.length === 0 ? (
                <p className="lemma-green">No uncited empirical claims detected.</p>
              ) : (
                <ul>
                  {report.uncited_factual_claims?.map((item: any, i: number) => (
                    <li
                      key={i}
                      className="lemma-clickable-issue"
                      onClick={() => onJumpToLine && onJumpToLine(item.file, item.line)}
                    >
                      <span>"{item.claim || item.sentence}"</span> — {item.file}:{item.line}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {report.abstract_only_citations?.length > 0 && (
              <section>
                <h3>Citations Grounded on Abstract Only</h3>
                <ul>
                  {report.abstract_only_citations.map((key: string, i: number) => (
                    <li key={i}><span className="lemma-badge-warning">Abstract Only</span>: <code>{key}</code></li>
                  ))}
                </ul>
              </section>
            )}

            {report.injection_warnings?.length > 0 && (
              <section>
                <h3>Prompt Injection Warnings</h3>
                <ul>
                  {report.injection_warnings.map((w: any, i: number) => (
                    <li key={i} className="lemma-red">Warning in {w.file || w.doc_id}: {JSON.stringify(w.matches)}</li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        )}

        <div className="lemma-modal-actions">
          <button
            type="button"
            className="lemma-btn lemma-btn-primary"
            onClick={runHealthCheck}
            disabled={loading}
          >
            Re-run Audit
          </button>
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary"
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

export interface VisualToLatexModalProps {
  projectId: string;
  csrfToken: string;
  isOpen: boolean;
  onClose: () => void;
  onHunkProposed?: (hunk: ProposedHunkItem) => void;
  filePath?: string;
}

export function VisualToLatexModal({
  projectId,
  csrfToken,
  isOpen,
  onClose,
  onHunkProposed,
  filePath = "main.tex",
}: VisualToLatexModalProps): React.ReactElement | null {
  const [tab, setTab] = useState<"csv" | "image">("csv");
  const [csvText, setCsvText] = useState<string>("");
  const [caption, setCaption] = useState<string>("");
  const [label, setLabel] = useState<string>("tab:results");
  const [targetFormat, setTargetFormat] = useState<"equation" | "booktabs" | "tikz">("equation");
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [result, setResult] = useState<any | null>(null);

  if (!isOpen) return null;

  const handleConvert = async () => {
    setLoading(true);
    setErrorMsg(null);
    setResult(null);

    try {
      let body: any = {
        caption,
        label,
        file_path: filePath,
        target_format: targetFormat,
      };

      if (tab === "csv") {
        if (!csvText.trim()) throw new Error("Please enter CSV or TSV data.");
        body.csv_text = csvText;
      } else {
        if (!imageFile) throw new Error("Please select an image file.");
        const reader = new FileReader();
        const base64Promise = new Promise<string>((resolve, reject) => {
          reader.onload = () => {
            const res = reader.result as string;
            const b64 = res.includes(",") ? res.split(",")[1] : res;
            resolve(b64);
          };
          reader.onerror = reject;
        });
        reader.readAsDataURL(imageFile);
        body.image_base64 = await base64Promise;
      }

      const res = await fetch(`/api/projects/${projectId}/tools/visual-to-latex`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(body),
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Conversion failed");
      }

      const data = await res.json();
      setResult(data);
      if (data.hunk && onHunkProposed) {
        onHunkProposed(data.hunk);
      }
    } catch (err: any) {
      setErrorMsg(err.message || String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="lemma-modal-backdrop" role="dialog" aria-label="Visual-to-LaTeX Converter">
      <div className="lemma-modal-card">
        <h2>Visual-to-LaTeX Converter</h2>
        <button type="button" aria-label="Close" className="lemma-modal-close" onClick={onClose}>
          ×
        </button>

        <div className="lemma-tab-row" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "csv"}
            onClick={() => setTab("csv")}
          >
            CSV to booktabs Table
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "image"}
            onClick={() => setTab("image")}
          >
            Screenshot/Image to LaTeX
          </button>
        </div>

        {tab === "csv" ? (
          <div className="lemma-tab-content">
            <label htmlFor="csv-data-input">CSV Data</label>
            <textarea
              id="csv-data-input"
              aria-label="CSV Data"
              rows={6}
              placeholder="Paste comma or tab-separated data here..."
              value={csvText}
              onChange={(e) => setCsvText(e.target.value)}
            />
          </div>
        ) : (
          <div className="lemma-tab-content">
            <label htmlFor="target-format-select">Target LaTeX Format</label>
            <select
              id="target-format-select"
              aria-label="Target Format"
              value={targetFormat}
              onChange={(e) => setTargetFormat(e.target.value as any)}
            >
              <option value="equation">Equation Math</option>
              <option value="booktabs">booktabs Table</option>
              <option value="tikz">TikZ Diagram</option>
            </select>

            <label htmlFor="image-file-input">Select Image</label>
            <input
              id="image-file-input"
              type="file"
              accept="image/*"
              aria-label="Select Image"
              onChange={(e) => setImageFile(e.target.files?.[0] || null)}
            />
          </div>
        )}

        <div className="lemma-form-row">
          <div>
            <label htmlFor="caption-input">Caption (Optional)</label>
            <input
              id="caption-input"
              type="text"
              aria-label="Caption"
              value={caption}
              onChange={(e) => setCaption(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="label-input">Label (Optional)</label>
            <input
              id="label-input"
              type="text"
              aria-label="Label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>
        </div>

        {errorMsg && <p className="lemma-error" role="alert">{errorMsg}</p>}

        {result && (
          <div className="lemma-convert-result">
            <h3>Generated LaTeX</h3>
            <pre><code>{result.latex_code}</code></pre>
            {result.lint_warnings?.length > 0 && (
              <div className="lemma-lint-warnings">
                <strong>Lint Warnings:</strong>
                <ul>
                  {result.lint_warnings.map((w: string, i: number) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              </div>
            )}
            {result.hunk && (
              <p className="lemma-green">
                Proposed hunk created! Review and accept it in the inline editor diff.
              </p>
            )}
          </div>
        )}

        <div className="lemma-modal-actions">
          <button
            type="button"
            className="lemma-btn lemma-btn-primary"
            onClick={handleConvert}
            disabled={loading}
          >
            {loading ? "Converting..." : "Convert to LaTeX"}
          </button>
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary"
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

export interface ExportModalProps {
  projectId: string;
  csrfToken: string;
  isOpen: boolean;
  onClose: () => void;
  rootTex?: string;
}

export function ExportModal({
  projectId,
  csrfToken,
  isOpen,
  onClose,
  rootTex = "main.tex",
}: ExportModalProps): React.ReactElement | null {
  const [loading, setLoading] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [exportData, setExportData] = useState<{
    verificationPassed: boolean;
    includedFiles: string[];
    strippedFiles: string[];
    blob?: Blob;
  } | null>(null);

  if (!isOpen) return null;

  const handleExport = async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const res = await fetch(`/api/projects/${projectId}/export`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ root_tex: rootTex }),
      });

      if (!res.ok) {
        throw new Error(`Export failed with status ${res.status}`);
      }

      const verifiedPassed = res.headers.get("x-verification-passed") === "true";
      const includedStr = res.headers.get("x-included-files") || "[]";
      const strippedStr = res.headers.get("x-stripped-files") || "[]";
      const blob = await res.blob();

      setExportData({
        verificationPassed: verifiedPassed,
        includedFiles: JSON.parse(includedStr),
        strippedFiles: JSON.parse(strippedStr),
        blob,
      });
    } catch (err: any) {
      setErrorMsg(err.message || String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleDownload = () => {
    if (!exportData?.blob) return;
    const url = URL.createObjectURL(exportData.blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${projectId}_camera_ready.zip`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => {
      URL.revokeObjectURL(url);
    }, 1000);
  };

  return (
    <div className="lemma-modal-backdrop" role="dialog" aria-label="Export Camera-Ready .zip">
      <div className="lemma-modal-card">
        <h2>Export Camera-Ready Submission (.zip)</h2>
        <button type="button" aria-label="Close" className="lemma-modal-close" onClick={onClose}>
          ×
        </button>

        <p>
          Packages traced manuscript files (.tex, .bib, .bbl, custom .sty/.cls), strips unused assets and reviews,
          and performs a standalone isolated verification compile with empty TEXMFHOME.
        </p>

        {loading && <p>Compiling project, tracing file-open log, and verifying standalone archive...</p>}
        {errorMsg && <p className="lemma-error" role="alert">{errorMsg}</p>}

        {exportData && (
          <div className="lemma-export-report">
            <div className="lemma-verification-badge-row">
              <strong>Standalone Verification:</strong>{" "}
              {exportData.verificationPassed ? (
                <span className="lemma-badge-success">Verification Passed</span>
              ) : (
                <span className="lemma-badge-danger">Verification Failed</span>
              )}
            </div>

            <section>
              <h3>Included Files ({exportData.includedFiles.length})</h3>
              <ul className="lemma-file-tag-list">
                {exportData.includedFiles.map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            </section>

            <section>
              <h3>Stripped Files ({exportData.strippedFiles.length})</h3>
              <ul className="lemma-file-tag-list">
                {exportData.strippedFiles.map((f, i) => (
                  <li key={i} className="lemma-stripped-file">{f}</li>
                ))}
              </ul>
            </section>

            <div className="lemma-download-box">
              <button type="button" onClick={handleDownload} className="lemma-btn-primary">
                Download .zip Archive
              </button>
            </div>
          </div>
        )}

        <div className="lemma-modal-actions">
          {!exportData && (
            <button
              type="button"
              onClick={handleExport}
              disabled={loading}
              className="lemma-btn lemma-btn-primary"
            >
              {loading ? "Exporting..." : "Export Camera-Ready .zip"}
            </button>
          )}
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary"
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

export interface ToolsMenuModalProps {
  projectId: string;
  csrfToken: string;
  isOpen: boolean;
  initialTab?: "health_check" | "visual_to_latex" | "export";
  onClose: () => void;
  onHunkProposed?: (hunk: ProposedHunkItem) => void;
  onJumpToLine?: (filePath: string, line: number) => void;
  rootTex?: string;
  filePath?: string;
}

export function ToolsMenuModal({
  projectId,
  csrfToken,
  isOpen,
  initialTab = "health_check",
  onClose,
  onHunkProposed,
  onJumpToLine,
  rootTex = "main.tex",
  filePath = "main.tex",
}: ToolsMenuModalProps): React.ReactElement | null {
  if (!isOpen) return null;

  return (
    <>
      {initialTab === "health_check" && (
        <HealthCheckModal
          projectId={projectId}
          csrfToken={csrfToken}
          isOpen={true}
          onClose={onClose}
          onJumpToLine={onJumpToLine}
          rootTex={rootTex}
        />
      )}
      {initialTab === "visual_to_latex" && (
        <VisualToLatexModal
          projectId={projectId}
          csrfToken={csrfToken}
          isOpen={true}
          onClose={onClose}
          onHunkProposed={onHunkProposed}
          filePath={filePath}
        />
      )}
      {initialTab === "export" && (
        <ExportModal
          projectId={projectId}
          csrfToken={csrfToken}
          isOpen={true}
          onClose={onClose}
          rootTex={rootTex}
        />
      )}
    </>
  );
}
