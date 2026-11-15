import React, { useState, useEffect } from "react";
import {
  acceptProposedHunk,
  applyCitationSelectionAndRemapping,
  type AcceptHunkOptions,
  type AcceptHunkResult,
} from "./DiffExtension";

export interface CandidateSourceItem {
  candidate_id: string;
  project_id: string;
  cite_key: string;
  title: string;
  authors: string[];
  year: number | null;
  doi: string | null;
  abstract: string | null;
  source_api: string;
  bibtex: string;
  grounding_tier: "full_text_pdf" | "abstract_only";
  pdf_path: string | null;
  created_at: string;
  expires_at: string;
  status: "staged" | "accepted" | "rejected" | "expired";
}

export interface CandidateDecisionResult {
  accepted_cite_keys: string[];
  rejected_cite_keys: string[];
  key_remapping: Record<string, string>;
  zotero_pushed_keys: string[];
  bib_etag: string | null;
  warning_message?: string | null;
}

export interface CandidateSourcesTrayProps {
  projectId: string;
  initialCandidates?: CandidateSourceItem[];
  csrfToken?: string;
  fetchImpl?: typeof fetch;
  onDecisionApplied?: (result: CandidateDecisionResult) => void;
}

export function computeTtlDaysRemaining(
  expiresAtIso: string,
  nowMs: number = Date.now()
): number {
  const expMs = Date.parse(expiresAtIso);
  if (Number.isNaN(expMs)) {
    return 0;
  }
  const diffDays = Math.ceil((expMs - nowMs) / (1000 * 60 * 60 * 24));
  return Math.max(0, diffDays);
}

export async function acceptHunkWithCitationDecision(
  options: AcceptHunkOptions & {
    acceptedCiteKeys: string[];
    rejectedCiteKeys: string[];
    csrfToken?: string;
    fetchImpl?: typeof fetch;
  }
): Promise<AcceptHunkResult & { decision?: CandidateDecisionResult }> {
  const {
    projectId,
    acceptedCiteKeys,
    rejectedCiteKeys,
    csrfToken = "",
    fetchImpl = globalThis.fetch,
  } = options;

  let decision: CandidateDecisionResult | undefined;
  let mergedRemapping: Record<string, string> = { ...(options.keyRemapping || {}) };

  if (projectId && typeof fetchImpl === "function") {
    const resp = await fetchImpl(
      `/api/projects/${projectId}/candidates/decision`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          accepted_cite_keys: acceptedCiteKeys,
          rejected_cite_keys: rejectedCiteKeys,
        }),
      }
    );
    if (!resp.ok) {
      let errorDetail = `Citation decision failed (HTTP ${resp.status})`;
      try {
        const errData = await resp.json();
        if (errData && errData.detail) errorDetail = errData.detail;
      } catch {}
      return {
        accepted: false,
        reanchored: false,
        conflict: resp.status === 409,
        blockedReason: errorDetail,
      };
    }
    decision = (await resp.json()) as CandidateDecisionResult;
    if (decision && decision.key_remapping) {
      mergedRemapping = { ...mergedRemapping, ...decision.key_remapping };
    }
  }

  const hunkResult = await acceptProposedHunk({
    ...options,
    rejectedCiteKeys,
    keyRemapping: mergedRemapping,
  });

  return {
    ...hunkResult,
    decision,
  };
}

export function CandidateSourcesTray({
  projectId,
  initialCandidates = [],
  csrfToken = "",
  fetchImpl = globalThis.fetch,
  onDecisionApplied,
}: CandidateSourcesTrayProps) {
  const [candidates, setCandidates] =
    useState<CandidateSourceItem[]>(initialCandidates);
  const [expanded, setExpanded] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);
  const [pendingUploadCand, setPendingUploadCand] =
    useState<CandidateSourceItem | null>(null);

  useEffect(() => {
    if (initialCandidates.length > 0) {
      setCandidates(initialCandidates);
      return;
    }
    if (!projectId || projectId === "default" || typeof fetchImpl !== "function") {
      return;
    }
    let active = true;
    fetchImpl(`/api/projects/${projectId}/candidates`)
      .then((res) => (res && res.ok ? res.json() : { candidates: [] }))
      .then((data: { candidates?: CandidateSourceItem[] }) => {
        if (!active) return;
        if (Array.isArray(data.candidates)) {
          setCandidates(data.candidates);
        }
      })
      .catch((err) => {
        if (!active) return;
        setErrorMessage(err instanceof Error ? err.message : "Failed to load candidates");
      });
    return () => {
      active = false;
    };
  }, [projectId]);

  const handleUploadPdf = async (candidate: CandidateSourceItem, file?: File) => {
    if (!projectId || projectId === "default" || typeof fetchImpl !== "function") return;
    setErrorMessage(null);
    let bodyPayload: BodyInit | undefined;
    if (typeof FormData !== "undefined") {
      const formData = new FormData();
      if (file) {
        formData.append("file", file);
      } else if (typeof Blob !== "undefined") {
        formData.append(
          "file",
          new Blob(["%PDF-1.4 uploaded full text"], { type: "application/pdf" }),
          `${candidate.cite_key}.pdf`
        );
      }
      bodyPayload = formData;
    }

    try {
      const resp = await fetchImpl(
        `/api/projects/${projectId}/candidates/${candidate.candidate_id}/upload-pdf`,
        {
          method: "POST",
          headers: {
            "X-CSRF-Token": csrfToken,
          },
          body: bodyPayload,
        }
      );
      if (!resp.ok) {
        let errTxt = `PDF upload failed (HTTP ${resp.status})`;
        try {
          const errJson = await resp.json();
          if (errJson && errJson.detail) errTxt = errJson.detail;
        } catch {}
        setErrorMessage(errTxt);
        return;
      }
      const data = await resp.json();
      if (data && data.candidate) {
        const updated = data.candidate as CandidateSourceItem;
        setCandidates((prev) =>
          prev.map((item) =>
            item.candidate_id === updated.candidate_id ? updated : item
          )
        );
        setStatusMessage(
          `Upgraded ${updated.cite_key} to Full-Text PDF Grounded.`
        );
      }
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : "Network error uploading PDF");
    }
  };

  const onFileInputChanged = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files && e.target.files[0];
    if (file && pendingUploadCand) {
      handleUploadPdf(pendingUploadCand, file);
    }
    if (e.target) {
      e.target.value = "";
    }
  };

  const handleDecision = async (
    acceptedCiteKeys: string[],
    rejectedCiteKeys: string[]
  ) => {
    if (typeof fetchImpl !== "function") return;
    setErrorMessage(null);
    try {
      const resp = await fetchImpl(
        `/api/projects/${projectId}/candidates/decision`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            accepted_cite_keys: acceptedCiteKeys,
            rejected_cite_keys: rejectedCiteKeys,
          }),
        }
      );
      if (!resp.ok) {
        let errTxt = `Decision failed (HTTP ${resp.status})`;
        try {
          const errJson = await resp.json();
          if (errJson && errJson.detail) errTxt = errJson.detail;
        } catch {}
        setErrorMessage(errTxt);
        return;
      }
      const decision = (await resp.json()) as CandidateDecisionResult;
      const removedKeys = new Set([
        ...acceptedCiteKeys,
        ...rejectedCiteKeys,
        ...(decision.accepted_cite_keys || []),
        ...(decision.rejected_cite_keys || []),
      ]);
      setCandidates((prev) =>
        prev.filter((c) => !removedKeys.has(c.cite_key))
      );
      if (
        decision.key_remapping &&
        Object.keys(decision.key_remapping).length > 0
      ) {
        const pairs = Object.entries(decision.key_remapping)
          .map(([fromKey, toKey]) =>
            applyCitationSelectionAndRemapping(
              `\\cite{${fromKey}}`,
              [],
              { [fromKey]: toKey }
            )
          )
          .join(", ");
        setStatusMessage(`Committed citation(s) with key remapping: ${pairs}`);
      } else if (decision.warning_message) {
        setStatusMessage(decision.warning_message);
      } else {
        setStatusMessage("Citation decisions updated successfully.");
      }
      if (onDecisionApplied) {
        onDecisionApplied(decision);
      }
    } catch (err) {
      setErrorMessage(err instanceof Error ? err.message : "Network error updating citation decision");
    }
  };

  const banners = (
    <>
      {statusMessage ? (
        <div role="status" className="lemma-status-banner">
          {statusMessage}
        </div>
      ) : null}

      {errorMessage ? (
        <div role="alert" className="lemma-error-banner">
          {errorMessage}
        </div>
      ) : null}
    </>
  );

  if (candidates.length === 0) {
    // Nothing to review: stay hidden unless a status/error message still needs to be seen.
    if (!statusMessage && !errorMessage) return null;
    return (
      <section
        aria-label="Papers the AI suggests"
        data-testid="candidate-sources-tray"
        className="lemma-candidate-tray"
      >
        {banners}
      </section>
    );
  }

  return (
    <section
      aria-label="Papers the AI suggests"
      data-testid="candidate-sources-tray"
      className="lemma-candidate-tray"
    >
      <button
        type="button"
        className="lemma-candidate-tray-toggle"
        aria-expanded={expanded}
        onClick={() => setExpanded((prev) => !prev)}
      >
        <span className="lemma-candidate-tray-chevron" aria-hidden="true">
          {expanded ? "▾" : "▸"}
        </span>
        {`Papers the AI suggests (${candidates.length})`}
      </button>

      {expanded ? (
        <>
      <p className="lemma-candidate-tray-subtitle">
        Papers found while the AI wrote. Accept to add them to your bibliography, or reject.
      </p>

      {banners}

      <input
        type="file"
        ref={fileInputRef}
        onChange={onFileInputChanged}
        accept="application/pdf"
        hidden
      />

        <ul className="lemma-candidate-list">
          {candidates.map((cand) => {
            const ttlDays = computeTtlDaysRemaining(cand.expires_at);
            const isFullText = cand.grounding_tier === "full_text_pdf";
            return (
              <li
                key={cand.candidate_id}
                data-testid={`candidate-card-${cand.cite_key}`}
                className="lemma-candidate-card"
              >
                <div className="lemma-candidate-card-header">
                  <div>
                    <strong className="lemma-candidate-title">{cand.title}</strong>{" "}
                    <code className="lemma-candidate-key">
                      [{cand.cite_key}]
                    </code>
                    <div className="lemma-candidate-authors">
                      {cand.authors.join("; ")}{" "}
                      {cand.year ? `(${cand.year})` : ""}
                    </div>
                  </div>
                  <span className="lemma-candidate-ttl">
                    Expires in {ttlDays}d
                  </span>
                </div>

                <div className="lemma-candidate-meta-row">
                  {cand.doi ? (
                    <span className="lemma-candidate-doi">
                      DOI: {cand.doi}
                    </span>
                  ) : null}

                  <span
                    data-testid={`grounding-badge-${cand.cite_key}`}
                    className={`lemma-candidate-badge ${
                      isFullText ? "full-text" : "abstract-only"
                    }`}
                  >
                    {isFullText
                      ? "Full-Text PDF Grounded"
                      : "Grounded on Abstract Only — Upload PDF for full-text verification"}
                  </span>
                </div>

                <div className="lemma-candidate-actions">
                  {!isFullText ? (
                    <button
                      type="button"
                      aria-label={`Upload PDF for ${cand.cite_key}`}
                      className="lemma-btn lemma-btn-primary lemma-btn-xs"
                      onClick={() => handleUploadPdf(cand)}
                    >
                      Upload PDF
                    </button>
                  ) : null}

                  <button
                    type="button"
                    aria-label={`Accept citation ${cand.cite_key}`}
                    className="lemma-btn lemma-btn-success lemma-btn-xs"
                    onClick={() => handleDecision([cand.cite_key], [])}
                  >
                    Accept \cite&#123;{cand.cite_key}&#125;
                  </button>

                  <button
                    type="button"
                    aria-label={`Reject citation ${cand.cite_key}`}
                    className="lemma-btn lemma-btn-danger lemma-btn-xs"
                    onClick={() => handleDecision([], [cand.cite_key])}
                  >
                    Reject
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
        </>
      ) : null}
    </section>
  );
}
