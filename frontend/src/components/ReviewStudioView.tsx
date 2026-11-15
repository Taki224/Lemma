import React, { useState, useEffect, useRef } from "react";
import { SanitizedMarkdown } from "./FloatingChatBar";
import { ModelSelect } from "./ModelSelect";
import { useEnabledModels } from "../context/EnabledModelsContext";

export interface ReviewIssueItem {
  issue_id: string;
  reviewer_persona: string;
  model: string;
  severity: "major" | "minor" | "nit";
  file_path: string;
  original_line?: number | null;
  resolved_line?: number | null;
  page_number?: number | null;
  quoted_anchor: string;
  title: string;
  critique: string;
  suggested_fix?: string | null;
  stale?: boolean;
}

export interface ReviewReportSummary {
  report_id: string;
  snapshot_sha: string;
  timestamp: string;
  issue_count: number;
}

export interface ReviewCostEstimate {
  page_count: number;
  estimated_prompt_tokens: number;
  estimated_completion_tokens: number;
  estimated_usd: number;
  requires_confirmation: boolean;
  details?: Record<string, any>;
}

export interface ReviewStudioViewProps {
  projectId: string;
  csrfToken: string;
  onJumpToLine?: (filePath: string, line: number) => void;
  onJumpToPage?: (page: number) => void;
  onProposeFix?: (issue: ReviewIssueItem) => void;
  onClose?: () => void;
}

export function ReviewStudioView({
  projectId,
  csrfToken,
  onJumpToLine,
  onJumpToPage,
  onProposeFix,
  onClose,
}: ReviewStudioViewProps): React.ReactElement {
  const { models: enabledModels } = useEnabledModels();
  const [methodologyModel, setMethodologyModel] = useState<string>("");
  const [literatureModel, setLiteratureModel] = useState<string>("");
  const [structureModel, setStructureModel] = useState<string>("");
  const [includeVisual, setIncludeVisual] = useState<boolean>(true);
  const [visionModel, setVisionModel] = useState<string>("");
  const hasVisionModel = enabledModels.some((m) => m.supports_vision);
  const visualActive = includeVisual && hasVisionModel;
  const modelsReady =
    Boolean(methodologyModel && literatureModel && structureModel) &&
    (!visualActive || Boolean(visionModel));

  const [estimate, setEstimate] = useState<ReviewCostEstimate | null>(null);
  const [estimating, setEstimating] = useState<boolean>(false);
  const [running, setRunning] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const [showConfirmModal, setShowConfirmModal] = useState<boolean>(false);
  const [reports, setReports] = useState<ReviewReportSummary[]>([]);
  const [selectedReportId, setSelectedReportId] = useState<string | null>(null);
  const [issues, setIssues] = useState<ReviewIssueItem[]>([]);
  const [severityFilter, setSeverityFilter] = useState<"all" | "major" | "minor" | "nit">("all");
  const isMounted = useRef(true);

  useEffect(() => {
    isMounted.current = true;
    return () => {
      isMounted.current = false;
    };
  }, []);

  // Load existing reports on mount
  useEffect(() => {
    let active = true;
    fetch(`/api/projects/${projectId}/reviews`)
      .then((res) => res.json())
      .then((data: { reports?: ReviewReportSummary[] }) => {
        if (!active || !isMounted.current) return;
        const list = data.reports || [];
        setReports(list);
        if (list.length > 0 && !selectedReportId) {
          loadReport(list[0].report_id);
        }
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [projectId]);

  const loadReport = (reportId: string) => {
    setSelectedReportId(reportId);
    fetch(`/api/projects/${projectId}/reviews/${reportId}`)
      .then((res) => res.json())
      .then((data: { issues?: ReviewIssueItem[] }) => {
        if (!isMounted.current) return;
        setIssues(data.issues || []);
      })
      .catch((err) => {
        if (!isMounted.current) return;
        setErrorMessage(String(err));
      });
  };

  const handleEstimate = async () => {
    if (!modelsReady) return;
    setEstimating(true);
    setErrorMessage(null);
    try {
      const assignments = [
        { persona_id: "methodology_critic", model: methodologyModel },
        { persona_id: "literature_auditor", model: literatureModel },
        { persona_id: "structure_editor", model: structureModel },
      ];
      const res = await fetch(`/api/projects/${projectId}/reviews/estimate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          assignments,
          include_visual: visualActive,
          vision_model: visualActive ? visionModel : null,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Estimation failed" }));
        throw new Error(err.detail || "Estimation failed");
      }
      const data: ReviewCostEstimate = await res.json();
      if (!isMounted.current) return;
      setEstimate(data);
    } catch (err: any) {
      if (!isMounted.current) return;
      setErrorMessage(err.message || String(err));
    } finally {
      if (isMounted.current) {
        setEstimating(false);
      }
    }
  };

  const handleStartRun = () => {
    if (!modelsReady) return;
    if (estimate?.requires_confirmation) {
      setShowConfirmModal(true);
    } else {
      executeRun(false);
    }
  };

  const executeRun = async (confirmHighCost: boolean) => {
    setShowConfirmModal(false);
    setRunning(true);
    setErrorMessage(null);
    try {
      const assignments = [
        { persona_id: "methodology_critic", model: methodologyModel },
        { persona_id: "literature_auditor", model: literatureModel },
        { persona_id: "structure_editor", model: structureModel },
      ];
      const res = await fetch(`/api/projects/${projectId}/reviews/run`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          assignments,
          include_visual: visualActive,
          vision_model: visualActive ? visionModel : null,
          confirm_high_cost: confirmHighCost,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Review run failed" }));
        throw new Error(err.detail || "Review run failed");
      }
      const data = await res.json();
      if (!isMounted.current) return;
      setSelectedReportId(data.report_id);
      setIssues(data.issues || []);
      // Refresh report list
      fetch(`/api/projects/${projectId}/reviews`)
        .then((r) => r.json())
        .then((d) => {
          if (isMounted.current) {
            setReports(d.reports || []);
          }
        })
        .catch(() => {});
    } catch (err: any) {
      if (!isMounted.current) return;
      setErrorMessage(err.message || String(err));
    } finally {
      if (isMounted.current) {
        setRunning(false);
      }
    }
  };

  const filteredIssues = issues.filter((iss) => {
    if (severityFilter === "all") return true;
    return iss.severity === severityFilter;
  });

  return (
    <div className="lemma-review-studio" data-testid="review-studio-view">
      <header className="lemma-review-studio-header">
        <div className="lemma-review-studio-title-block">
          <h2>Academic Peer Review Studio</h2>
          <span className="lemma-review-studio-subtitle">
            Multi-Model Adversarial Persona Review & Visual PDF Typesetting Inspection
          </span>
        </div>
        {onClose && (
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary"
            onClick={onClose}
            data-testid="close-review-studio-btn"
          >
            Back to Editor
          </button>
        )}
      </header>

      {errorMessage && (
        <div className="lemma-error-banner" data-testid="review-error-banner">
          {errorMessage}
        </div>
      )}

      {/* Control Panel: Model assignments and Cost Estimation */}
      <section className="lemma-review-controls" data-testid="review-controls-section">
        <div className="lemma-persona-selectors-grid">
          <div className="lemma-selector-card">
            <ModelSelect
              id="methodology-model-select"
              label="Methodology & Logic Critic"
              value={methodologyModel}
              onChange={setMethodologyModel}
              testId="methodology-model-select"
            />
          </div>

          <div className="lemma-selector-card">
            <ModelSelect
              id="literature-model-select"
              label="Literature & Grounding Auditor"
              value={literatureModel}
              onChange={setLiteratureModel}
              testId="literature-model-select"
            />
          </div>

          <div className="lemma-selector-card">
            <ModelSelect
              id="structure-model-select"
              label="Structure & Clarity Editor"
              value={structureModel}
              onChange={setStructureModel}
              testId="structure-model-select"
            />
          </div>

          <div className="lemma-selector-card">
            <label className="lemma-checkbox-label">
              <input
                type="checkbox"
                data-testid="include-visual-checkbox"
                checked={visualActive}
                disabled={!hasVisionModel}
                onChange={(e) => setIncludeVisual(e.target.checked)}
              />
              <span>Visual PDF Layout Inspector</span>
            </label>
            {(visualActive || !hasVisionModel) && (
              <ModelSelect
                id="vision-model-select"
                label="Vision Model"
                value={visionModel}
                onChange={setVisionModel}
                visionOnly
                testId="vision-model-select"
              />
            )}
          </div>
        </div>

        <div className="lemma-review-action-bar">
          <div className="lemma-estimate-display">
            <button
              type="button"
              className="lemma-btn lemma-btn-secondary"
              data-testid="estimate-cost-btn"
              onClick={handleEstimate}
              disabled={estimating || running || !modelsReady}
            >
              {estimating ? "Estimating..." : "Estimate Cost"}
            </button>
            {estimate && (
              <div className="lemma-estimate-badges">
                <span className="lemma-badge-estimate" data-testid="estimate-usd-badge">
                  ${(estimate.estimated_usd ?? 0).toFixed(2)} USD ({estimate.page_count} pages)
                </span>
                {estimate.requires_confirmation && (
                  <span className="lemma-badge-confirm-req" data-testid="requires-confirm-badge">
                    Confirmation Required (&gt;= $0.50)
                  </span>
                )}
              </div>
            )}
          </div>

          <button
            type="button"
            className="lemma-btn lemma-btn-primary"
            data-testid="run-review-btn"
            onClick={handleStartRun}
            disabled={running || estimating || !modelsReady}
          >
            {running ? "Reviewing..." : "Run Multi-Model Review"}
          </button>
        </div>
      </section>

      {/* Confirmation Modal */}
      {showConfirmModal && (
        <div className="lemma-modal-backdrop" data-testid="cost-confirmation-modal">
          <div className="lemma-modal-dialog">
            <h3>Confirm High-Cost Review Run</h3>
            <p>
              The estimated cost for this multi-model peer review run is{" "}
              <strong>${(estimate?.estimated_usd ?? 0).toFixed(2)} USD</strong> ({estimate?.page_count ?? 0} pages).
            </p>
            <p className="lemma-subtext">
              Review runs exceeding $0.50 require explicit confirmation before launching parallel AI reviewer models.
            </p>
            <div className="lemma-modal-actions">
              <button
                type="button"
                className="lemma-btn lemma-btn-secondary"
                data-testid="cancel-confirmation-btn"
                onClick={() => setShowConfirmModal(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="lemma-btn lemma-btn-primary"
                data-testid="confirm-high-cost-btn"
                onClick={() => executeRun(true)}
              >
                Confirm &amp; Run (${(estimate?.estimated_usd ?? 0).toFixed(2)})
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Reports & Issues Section */}
      <section className="lemma-review-results-section" data-testid="review-results-section">
        <div className="lemma-results-toolbar">
          {reports.length > 0 && (
            <div className="lemma-report-picker">
              <label htmlFor="saved-reports-select">Saved Reports:</label>
              <select
                id="saved-reports-select"
                data-testid="saved-reports-select"
                value={selectedReportId || ""}
                onChange={(e) => loadReport(e.target.value)}
              >
                {reports.map((r) => (
                  <option key={r.report_id} value={r.report_id}>
                    {r.report_id} ({r.issue_count} issues, {r.snapshot_sha.slice(0, 7)})
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="lemma-severity-filters">
            <button
              type="button"
              className={`lemma-filter-btn ${severityFilter === "all" ? "active" : ""}`}
              onClick={() => setSeverityFilter("all")}
            >
              All ({issues.length})
            </button>
            <button
              type="button"
              className={`lemma-filter-btn major ${severityFilter === "major" ? "active" : ""}`}
              onClick={() => setSeverityFilter("major")}
            >
              Major ({issues.filter((i) => i.severity === "major").length})
            </button>
            <button
              type="button"
              className={`lemma-filter-btn minor ${severityFilter === "minor" ? "active" : ""}`}
              onClick={() => setSeverityFilter("minor")}
            >
              Minor ({issues.filter((i) => i.severity === "minor").length})
            </button>
            <button
              type="button"
              className={`lemma-filter-btn nit ${severityFilter === "nit" ? "active" : ""}`}
              onClick={() => setSeverityFilter("nit")}
            >
              Nit ({issues.filter((i) => i.severity === "nit").length})
            </button>
          </div>
        </div>

        <div className="lemma-issues-container">
          {filteredIssues.length === 0 ? (
            <div className="lemma-empty-state">
              {issues.length === 0
                ? "No peer review findings yet. Configure reviewer models above and click 'Run Multi-Model Review'."
                : `No issues matching severity filter '${severityFilter}'.`}
            </div>
          ) : (
            filteredIssues.map((iss) => (
              <div
                key={iss.issue_id}
                className={`lemma-issue-card lemma-issue-${iss.severity}`}
                data-testid="review-issue-card"
              >
                <div className="lemma-issue-header">
                  <div className="lemma-issue-badges">
                    <span className={`lemma-severity-badge lemma-badge-${iss.severity}`}>
                      {iss.severity.toUpperCase()}
                    </span>
                    <span className="lemma-persona-badge">
                      {iss.reviewer_persona} ({iss.model.split("/").pop()})
                    </span>
                    {iss.stale && (
                      <span className="lemma-badge-stale" data-testid="stale-badge">
                        Stale
                      </span>
                    )}
                  </div>
                  <div className="lemma-issue-actions">
                    {iss.file_path && iss.resolved_line && (
                      <button
                        type="button"
                        className="lemma-btn lemma-btn-small"
                        data-testid={`jump-line-btn-${iss.issue_id}`}
                        onClick={() => onJumpToLine?.(iss.file_path, iss.resolved_line!)}
                      >
                        Jump to {iss.file_path}:{iss.resolved_line}
                      </button>
                    )}
                    {iss.page_number && (
                      <button
                        type="button"
                        className="lemma-btn lemma-btn-small"
                        data-testid={`jump-page-btn-${iss.issue_id}`}
                        onClick={() => onJumpToPage?.(iss.page_number!)}
                      >
                        Jump to Page {iss.page_number}
                      </button>
                    )}
                  </div>
                </div>

                <h4 className="lemma-issue-title">{iss.title}</h4>

                {iss.quoted_anchor && (
                  <blockquote className="lemma-quoted-anchor">
                    &ldquo;{iss.quoted_anchor}&rdquo;
                  </blockquote>
                )}

                <div className="lemma-issue-critique">
                  <SanitizedMarkdown content={iss.critique} />
                </div>

                {iss.suggested_fix && (
                  <div className="lemma-suggested-fix-box">
                    <strong>Suggested Fix:</strong>
                    <div className="lemma-fix-text">{iss.suggested_fix}</div>
                    {onProposeFix && (
                      <button
                        type="button"
                        className="lemma-btn lemma-btn-small lemma-btn-primary"
                        data-testid={`propose-fix-btn-${iss.issue_id}`}
                        onClick={() => onProposeFix(iss)}
                      >
                        Propose Fix
                      </button>
                    )}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </section>
    </div>
  );
}
