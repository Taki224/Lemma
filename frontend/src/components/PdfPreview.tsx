import React, { useState, useEffect, useRef } from "react";
import * as pdfjsLib from "pdfjs-dist";
import type { LatexDiagnosticItem } from "./LatexEditor";

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url
).toString();

const HIGHLIGHT_DISMISS_MS = 3000;
const SYNC_NOTICE_DISMISS_MS = 4000;
const ROTATED_SYNC_NOTICE = "Reset rotation to jump to source";
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 3.0;
const WHEEL_ZOOM_SENSITIVITY = 0.01;
// Largest pixel delta honoured per wheel event, so one notchy mouse wheel step cannot jump zoom by multiples.
const MAX_WHEEL_DELTA_PX = 50;
// WheelEvent.deltaMode 1 (lines) and 2 (pages) converted to pixels.
const WHEEL_LINE_PX = 16;
const WHEEL_PAGE_PX = 400;
const MIN_HIGHLIGHT_WIDTH_PT = 4;
const MIN_HIGHLIGHT_HEIGHT_PT = 10;
// 1× displays (many ultrawides) rasterize canvas text without hinting, so a
// bitmap that matches CSS pixels looks soft. Draw at least 2 device pixels
// per CSS pixel, and more when the screen is denser than that.
const MIN_PDF_OUTPUT_SCALE = 2;

function readPdfOutputScale(): number {
  const dpr = typeof window === "undefined" ? 1 : window.devicePixelRatio || 1;
  return Math.max(dpr, MIN_PDF_OUTPUT_SCALE);
}

export interface PdfTextItemLike {
  str?: string;
  hasEOL?: boolean;
}

export interface PdfTextStats {
  words: number;
  lines: number;
  characters: number;
  charactersExcludingSpaces: number;
}

type PdfStatsState = "idle" | "counting" | "ready" | "unavailable";

/** Word breaks follow PDF end-of-line markers. Those breaks are not added to the character totals. */
export function statsFromTextItems(itemsByPage: PdfTextItemLike[][]): PdfTextStats {
  let words = 0;
  let lines = 0;
  let characters = 0;
  let charactersExcludingSpaces = 0;

  for (const items of itemsByPage) {
    let wordText = "";
    let lineOpen = false;
    for (const item of items) {
      const str = typeof item?.str === "string" ? item.str : "";
      characters += str.length;
      charactersExcludingSpaces += str.replace(/\s/g, "").length;
      wordText += str;
      if (str.length > 0) lineOpen = true;
      if (item?.hasEOL) {
        lines += 1;
        lineOpen = false;
        wordText += "\n";
      }
    }
    if (lineOpen) lines += 1;
    const trimmed = wordText.trim();
    if (trimmed) words += trimmed.split(/\s+/).length;
  }

  return { words, lines, characters, charactersExcludingSpaces };
}

export function formatStatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatPdfStatisticsSummary(pageCount: number, stats: PdfTextStats | null): string {
  const pages = `${formatStatCount(pageCount)} ${pageCount === 1 ? "page" : "pages"}`;
  if (!stats) return pages;
  const words = `${formatStatCount(stats.words)} ${stats.words === 1 ? "word" : "words"}`;
  const characters = `${formatStatCount(stats.characters)} ${stats.characters === 1 ? "character" : "characters"}`;
  return `${pages} · ${words} · ${characters}`;
}

function pdfStatCell(state: PdfStatsState, stats: PdfTextStats | null, value: number): string {
  if (state === "counting") return "Counting…";
  if (state !== "ready" || !stats) return "Unavailable";
  return formatStatCount(value);
}

// The test runner cannot replace the pdf.js ESM export, so tests swap this opener.
export const pdfDocumentLoader = {
  open(url: string): { promise?: Promise<any>; destroy?: () => void } | null {
    const getDocFn = pdfjsLib.getDocument || (pdfjsLib as any).default?.getDocument;
    if (typeof getDocFn !== "function") return null;
    return getDocFn(url);
  },
};

export interface ForwardHighlightBox {
  page: number;
  x: number;
  y: number;
  h: number;
  v: number;
  width: number;
  height: number;
  nonce: number;
  exact: boolean;
  hint?: string | null;
}

export interface PdfPreviewProps {
  projectId: string;
  status: "idle" | "compiling" | "success" | "failed" | "timeout";
  pdfUrl: string | null;
  diagnostics?: LatexDiagnosticItem[];
  forwardHighlight?: ForwardHighlightBox | null;
  onJumpToLine?: (filePath: string, line: number) => void;
  // Inverse SyncTeX (PDF double-click) jumps; falls back to onJumpToLine when omitted.
  onSyncJump?: (filePath: string, line: number) => void;
  onProposeFix?: (diagnostic: LatexDiagnosticItem) => void | Promise<void>;
  onRecompile?: () => void;
  isStale?: boolean;
  syncNotice?: string | null;
}

export function PdfPreview({
  projectId,
  status,
  pdfUrl,
  diagnostics = [],
  forwardHighlight = null,
  onJumpToLine,
  onSyncJump,
  onProposeFix,
  onRecompile,
  isStale = false,
  syncNotice = null,
}: PdfPreviewProps): React.ReactElement {
  const [dropdownOpen, setDropdownOpen] = useState<boolean>(false);
  const [ignoreWarnings, setIgnoreWarnings] = useState<boolean>(() => {
    try {
      return typeof localStorage !== "undefined" && localStorage.getItem("lemma_ignore_compile_warnings") === "true";
    } catch {
      return false;
    }
  });
  const [fixingIdx, setFixingIdx] = useState<number | null>(null);
  const [fixError, setFixError] = useState<string | null>(null);
  const [zoomScale, setZoomScale] = useState<number>(1.0);
  const [outputScale, setOutputScale] = useState<number>(readPdfOutputScale);

  // Moving the window onto another display changes devicePixelRatio without a
  // zoom change. Browser page-zoom does the same. Redraw when either happens.
  useEffect(() => {
    let removeResolution: (() => void) | undefined;
    function listen() {
      removeResolution?.();
      removeResolution = undefined;
      if (typeof window.matchMedia !== "function") return;
      const query = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`);
      query.addEventListener?.("change", refresh);
      removeResolution = () => query.removeEventListener?.("change", refresh);
    }
    function refresh() {
      setOutputScale(readPdfOutputScale());
      listen();
    }
    refresh();
    if (typeof window.addEventListener === "function") window.addEventListener("resize", refresh);
    return () => {
      removeResolution?.();
      if (typeof window.removeEventListener === "function") window.removeEventListener("resize", refresh);
    };
  }, []);
  const [rotation, setRotation] = useState<number>(0);
  const [pageCount, setPageCount] = useState<number>(1);
  const [textStats, setTextStats] = useState<PdfTextStats | null>(null);
  const [statsState, setStatsState] = useState<PdfStatsState>("idle");
  const [statsOpen, setStatsOpen] = useState<boolean>(false);
  const [fallbackToast, setFallbackToast] = useState<string | null>(
    forwardHighlight && !forwardHighlight.exact && forwardHighlight.hint
      ? forwardHighlight.hint
      : null
  );

  useEffect(() => {
    if (forwardHighlight && !forwardHighlight.exact && forwardHighlight.hint) {
      setFallbackToast(forwardHighlight.hint);
    } else if (forwardHighlight && forwardHighlight.exact) {
      setFallbackToast(null);
    }
  }, [forwardHighlight]);

  // The rotation notice is transient (also cleared when rotation returns to 0, see the Rotate button).
  useEffect(() => {
    if (fallbackToast !== ROTATED_SYNC_NOTICE) return;
    const timer = setTimeout(
      () => setFallbackToast((current) => (current === ROTATED_SYNC_NOTICE ? null : current)),
      SYNC_NOTICE_DISMISS_MS
    );
    return () => clearTimeout(timer);
  }, [fallbackToast]);

  const [dismissedSyncNotice, setDismissedSyncNotice] = useState<string | null>(null);
  useEffect(() => {
    if (!syncNotice) {
      setDismissedSyncNotice(null);
      return;
    }
    const timer = setTimeout(() => setDismissedSyncNotice(syncNotice), SYNC_NOTICE_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [syncNotice]);
  const visibleSyncNotice = syncNotice && syncNotice !== dismissedSyncNotice ? syncNotice : null;
  const toastText = visibleSyncNotice ?? fallbackToast;

  const [pdfDoc, setPdfDoc] = useState<any>(null);
  const loadingTaskRef = useRef<any>(null);
  const renderTasksRef = useRef<Map<number, any>>(new Map());
  const viewportContainerRef = useRef<HTMLDivElement | null>(null);
  const paneRef = useRef<HTMLDivElement | null>(null);
  const pageRefs = useRef<Map<number, HTMLDivElement | null>>(new Map());
  const pdfDocUrlRef = useRef<string | null>(null);
  const [dismissedNonce, setDismissedNonce] = useState<number | null>(null);

  const highlightNonce = forwardHighlight ? forwardHighlight.nonce : null;
  const highlightPage = forwardHighlight ? forwardHighlight.page : null;

  useEffect(() => {
    if (highlightNonce === null || highlightPage === null) return;
    const pageEl = pageRefs.current.get(highlightPage);
    if (pageEl && typeof pageEl.scrollIntoView === "function") {
      pageEl.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    const timer = setTimeout(() => setDismissedNonce(highlightNonce), HIGHLIGHT_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [highlightNonce, highlightPage]);

  useEffect(() => {
    if (!pdfUrl) {
      pdfDocUrlRef.current = null;
      setPdfDoc(null);
      setPageCount(1);
      setTextStats(null);
      setStatsState("idle");
      setStatsOpen(false);
      return;
    }
    setTextStats(null);
    setStatsState("counting");
    setStatsOpen(false);
    let active = true;
    let loadedDoc: any = null;
    try {
      const task = pdfDocumentLoader.open(pdfUrl);
      if (!task) {
        setStatsState("unavailable");
        return;
      }
      loadingTaskRef.current = task;

      task.promise
        ?.then((doc: any) => {
          if (!active) {
            try { doc?.destroy?.(); } catch {}
            return;
          }
          loadedDoc = doc;
          pdfDocUrlRef.current = pdfUrl;
          setPdfDoc(doc);
          if (doc?.numPages) {
            setPageCount(doc.numPages);
          }
        })
        ?.catch(() => {
          if (active) setStatsState("unavailable");
        });
    } catch {
      if (active) setStatsState("unavailable");
    }

    return () => {
      active = false;
      if (loadingTaskRef.current) {
        try {
          loadingTaskRef.current.destroy?.();
        } catch {}
      }
      if (loadedDoc) {
        try {
          loadedDoc.destroy?.();
        } catch {}
      }
      pdfDocUrlRef.current = null;
      setPdfDoc(null);
    };
  }, [pdfUrl]);

  useEffect(() => {
    if (!pdfDoc || pdfDocUrlRef.current !== pdfUrl) return;
    if (typeof pdfDoc.getPage !== "function") {
      setTextStats(null);
      setStatsState("unavailable");
      return;
    }
    let cancelled = false;
    const pages =
      typeof pdfDoc.numPages === "number" && pdfDoc.numPages > 0 ? pdfDoc.numPages : pageCount;

    (async () => {
      const itemsByPage: PdfTextItemLike[][] = [];
      for (let pageNum = 1; pageNum <= pages; pageNum++) {
        const page = await pdfDoc.getPage(pageNum);
        if (cancelled) return;
        if (!page || typeof page.getTextContent !== "function") {
          setTextStats(null);
          setStatsState("unavailable");
          return;
        }
        const content = await page.getTextContent();
        if (cancelled) return;
        itemsByPage.push(Array.isArray(content?.items) ? content.items : []);
      }
      setTextStats(statsFromTextItems(itemsByPage));
      setStatsState("ready");
    })().catch(() => {
      if (!cancelled) {
        setTextStats(null);
        setStatsState("unavailable");
      }
    });

    return () => {
      cancelled = true;
    };
  }, [pdfDoc, pageCount, pdfUrl]);

  useEffect(() => {
    const container = viewportContainerRef.current;
    if (!pdfUrl || !pdfDoc || !container || typeof pdfDoc.getPage !== "function") return;
    let cancelled = false;

    for (let pageNum = 1; pageNum <= pageCount; pageNum++) {
      pdfDoc
        .getPage(pageNum)
        ?.then((page: any) => {
          if (cancelled || !page || typeof page.getViewport !== "function") return;
          const viewport = page.getViewport({ scale: zoomScale * outputScale, rotation });
          const canvas = container.querySelector(
            `canvas[data-page-number="${pageNum}"]`
          ) as HTMLCanvasElement | null;
          if (!canvas) return;
          const bitmapWidth = viewport.width || 612 * zoomScale * outputScale;
          const bitmapHeight = viewport.height || 792 * zoomScale * outputScale;
          canvas.style.width = `${bitmapWidth / outputScale}px`;
          canvas.style.height = `${bitmapHeight / outputScale}px`;
          canvas.width = Math.floor(bitmapWidth);
          canvas.height = Math.floor(bitmapHeight);
          if (typeof canvas.getContext === "function") {
            const ctx = canvas.getContext("2d");
            if (ctx && typeof page.render === "function") {
              const existingTask = renderTasksRef.current.get(pageNum);
              if (existingTask && typeof existingTask.cancel === "function") {
                try {
                  existingTask.cancel();
                } catch {}
              }
              const renderTask = page.render({
                canvasContext: ctx,
                viewport,
              });
              renderTasksRef.current.set(pageNum, renderTask);
              renderTask.promise
                ?.then(() => {
                  if (renderTasksRef.current.get(pageNum) === renderTask) {
                    renderTasksRef.current.delete(pageNum);
                  }
                })
                ?.catch(() => {});
            }
          }
        })
        ?.catch(() => {});
    }

    return () => {
      cancelled = true;
      for (const task of renderTasksRef.current.values()) {
        try {
          task.cancel();
        } catch {}
      }
      renderTasksRef.current.clear();
    };
  }, [pdfUrl, pdfDoc, pageCount, zoomScale, rotation, outputScale]);

  // Native non-passive listener: React's onWheel is passive and cannot preventDefault,
  // which is needed to stop the browser's page-level pinch zoom. On the whole pane so the toolbar is covered too.
  useEffect(() => {
    const pane = paneRef.current;
    if (!pdfUrl || !pane || typeof pane.addEventListener !== "function") return;
    const handleWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      const unit = event.deltaMode === 1 ? WHEEL_LINE_PX : event.deltaMode === 2 ? WHEEL_PAGE_PX : 1;
      const deltaPx = Math.max(-MAX_WHEEL_DELTA_PX, Math.min(MAX_WHEEL_DELTA_PX, event.deltaY * unit));
      setZoomScale((z) =>
        Number(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z * Math.exp(-deltaPx * WHEEL_ZOOM_SENSITIVITY))).toFixed(2))
      );
    };
    pane.addEventListener("wheel", handleWheel, { passive: false });
    return () => {
      pane.removeEventListener("wheel", handleWheel);
    };
  }, [pdfUrl]);

  const statisticsSummary = formatPdfStatisticsSummary(
    pageCount,
    statsState === "ready" ? textStats : null
  );

  const errorCount = diagnostics.filter((d) => d.severity === "error").length;
  const warningCount = diagnostics.length - errorCount;
  const visibleDiagnostics = ignoreWarnings
    ? diagnostics.filter((d) => d.severity === "error")
    : diagnostics;
  const ignoredWarningCount = ignoreWarnings ? warningCount : 0;

  const handleToggleIgnoreWarnings = (checked: boolean) => {
    setIgnoreWarnings(checked);
    try {
      if (typeof localStorage !== "undefined") {
        localStorage.setItem("lemma_ignore_compile_warnings", checked ? "true" : "false");
      }
    } catch {
      // Ignore storage failures in restricted contexts.
    }
  };

  const handleProposeFixClick = async (diag: LatexDiagnosticItem, idx: number) => {
    if (!onProposeFix || fixingIdx !== null) return;
    setFixError(null);
    setFixingIdx(idx);
    try {
      await onProposeFix(diag);
    } catch (err) {
      setFixError(err instanceof Error ? err.message : "Failed to propose a fix.");
    } finally {
      setFixingIdx(null);
    }
  };

  const STATUS_LABELS: Record<string, string> = {
    compiling: "Compiling...",
    failed: "Compile Failed",
    timeout: "Timed Out",
  };

  const statusLabel =
    errorCount > 0
      ? errorCount === 1
        ? "1 Error"
        : `${errorCount} Errors`
      : status === "idle" && !pdfUrl
      ? "Not Compiled"
      : STATUS_LABELS[status] ?? "Compiled";

  const handlePageInverseSync = async (pageNumber: number, e: React.MouseEvent) => {
    if (rotation !== 0) {
      setFallbackToast(ROTATED_SYNC_NOTICE);
      return;
    }
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const relX = (e.clientX - rect.left) / zoomScale;
    const relY = (e.clientY - rect.top) / zoomScale;

    const query = new URLSearchParams({
      page: String(pageNumber),
      x: String(Math.round(relX * 10) / 10),
      y: String(Math.round(relY * 10) / 10),
    });

    const res = await fetch(`/api/projects/${projectId}/synctex/inverse?${query.toString()}`);
    if (!res.ok) return;
    const data = await res.json();
    if (!data.exact && data.hint) {
      setFallbackToast(data.hint);
    }
    const syncJump = onSyncJump ?? onJumpToLine;
    if (syncJump && data.file_path && data.line) {
      syncJump(data.file_path, Number(data.line));
    }
  };

  return (
    <div className="lemma-pdf-preview-pane" data-stale={isStale ? "true" : undefined} ref={paneRef}>
      <div className="lemma-pdf-toolbar">
        <div className="lemma-pdf-toolbar-cluster">
          <button
            type="button"
            className={`lemma-compile-status-badge status-${status}`}
            aria-label={`Compile Status: ${statusLabel}`}
            aria-expanded={dropdownOpen}
            onClick={() => setDropdownOpen((prev) => !prev)}
          >
            {statusLabel === "Compiled" && (
              <span className="lemma-compiled-check" aria-hidden="true">✓</span>
            )}
            {statusLabel}
            <span className="lemma-status-caret" aria-hidden="true">▾</span>
          </button>

          {onRecompile && (
            <button
              type="button"
              className="lemma-btn lemma-btn-secondary lemma-btn-xs"
              aria-label="Recompile"
              onClick={onRecompile}
              disabled={status === "compiling"}
            >
              {status === "compiling" ? "Compiling..." : "Recompile"}
            </button>
          )}
        </div>

        <div className="lemma-pdf-zoom-controls">
          <button
            type="button"
            aria-label="Zoom Out"
            onClick={() => setZoomScale((z) => Math.max(MIN_ZOOM, Number((z - 0.25).toFixed(2))))}
          >
            -
          </button>
          <span data-testid="pdf-zoom-level">{Math.round(zoomScale * 100)}%</span>
          <button
            type="button"
            aria-label="Zoom In"
            onClick={() => setZoomScale((z) => Math.min(MAX_ZOOM, Number((z + 0.25).toFixed(2))))}
          >
            +
          </button>
          <button
            type="button"
            aria-label="Zoom to fit"
            onClick={() => {
              const container = viewportContainerRef.current;
              const available = container ? container.clientWidth - 32 : 0;
              const next = available > 0 ? available / 612 : 1;
              setZoomScale(Number(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, next)).toFixed(2)));
            }}
          >
            Fit
          </button>
          <button
            type="button"
            aria-label="Rotate Page"
            onClick={() => {
              const nextRotation = (rotation + 90) % 360;
              setRotation(nextRotation);
              if (nextRotation === 0) {
                setFallbackToast((current) => (current === ROTATED_SYNC_NOTICE ? null : current));
              }
            }}
          >
            Rotate
          </button>
        </div>
      </div>

      {dropdownOpen && (
        <div
          className="lemma-compile-diagnostics-dropdown"
          role="region"
          aria-label="Compile Diagnostics"
        >
          {(warningCount > 0 || ignoreWarnings) && (
            <div className="lemma-compile-diagnostics-header">
              <label className="lemma-ignore-warnings-toggle">
                <input
                  type="checkbox"
                  checked={ignoreWarnings}
                  onChange={(e) => handleToggleIgnoreWarnings(e.target.checked)}
                />
                <span>Ignore warnings</span>
              </label>
            </div>
          )}
          {fixError && (
            <div className="lemma-diag-fix-error" role="alert">
              {fixError}
            </div>
          )}
          {visibleDiagnostics.length === 0 ? (
            <p>
              {status === "failed"
                ? "Compilation failed. No specific line diagnostics were produced by the compiler."
                : status === "timeout"
                ? "Compilation timed out before generating diagnostics."
                : ignoredWarningCount > 0
                ? `No compile errors (${ignoredWarningCount} ${ignoredWarningCount === 1 ? "warning" : "warnings"} ignored).`
                : "No compile errors or warnings."}
            </p>
          ) : (
            <ul>
              {visibleDiagnostics.map((diag, idx) => (
                <li key={idx} className={`lemma-diag-row severity-${diag.severity}`}>
                  <div className="lemma-diag-summary">
                    <strong>
                      {diag.file_path}:{diag.line ?? "?"}
                    </strong>{" "}
                    — {diag.message}
                  </div>
                  <div className="lemma-diag-actions">
                    <button
                      type="button"
                      className="lemma-btn lemma-btn-secondary lemma-btn-xs"
                      aria-label="Jump to line"
                      onClick={() => {
                        if (onJumpToLine) {
                          onJumpToLine(diag.file_path, diag.line || 1);
                        }
                      }}
                    >
                      Jump to line
                    </button>
                    {onProposeFix && (
                      <button
                        type="button"
                        className="lemma-btn lemma-btn-primary lemma-btn-xs"
                        aria-label="Propose fix"
                        disabled={fixingIdx !== null}
                        onClick={() => {
                          void handleProposeFixClick(diag, idx);
                        }}
                      >
                        {fixingIdx === idx ? "Proposing..." : "Propose fix"}
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {toastText && (
        <div role="status" className="lemma-synctex-fallback-toast" data-testid="synctex-fallback-toast">
          {toastText}
        </div>
      )}

      {!pdfUrl ? (
        <div className="lemma-pdf-empty-viewport">
          <div className="lemma-pdf-empty-card">
            <div className="lemma-pdf-empty-icon" aria-hidden="true">📄</div>
            <h3>PDF Preview</h3>
            <p>
              No PDF has been compiled for this project yet. Click &ldquo;Recompile&rdquo; or press Cmd+S / Ctrl+S in the editor to build your LaTeX manuscript.
            </p>
            {onRecompile && (
              <button
                type="button"
                className="lemma-btn lemma-btn-primary"
                onClick={onRecompile}
                disabled={status === "compiling"}
              >
                {status === "compiling" ? "Compiling..." : "Compile Manuscript"}
              </button>
            )}
          </div>
        </div>
      ) : (
        <div className="lemma-pdf-pages-viewport" data-rotation={rotation} ref={viewportContainerRef}>
          {Array.from({ length: pageCount }, (_, i) => i + 1).map((pageNum) => (
            <div
              key={pageNum}
              className="lemma-pdf-page-container"
              ref={(el: HTMLDivElement | null) => {
                pageRefs.current.set(pageNum, el);
              }}
              data-testid={`pdf-page-surface-${pageNum}`}
              title={rotation !== 0 ? ROTATED_SYNC_NOTICE : undefined}
              onDoubleClick={(e) => handlePageInverseSync(pageNum, e)}
            >
              <canvas className="lemma-pdf-canvas" data-page-number={pageNum} />
              {forwardHighlight &&
                forwardHighlight.page === pageNum &&
                forwardHighlight.nonce !== dismissedNonce &&
                rotation === 0 && (
                <div
                  className="lemma-synctex-highlight-overlay"
                  data-testid="synctex-highlight-box"
                  style={{
                    // SyncTeX: (h, v) is the box's left edge / baseline; (x, y) is the click-point
                    // of the matched node. The box spans from v - height (top) to v.
                    left: `${forwardHighlight.h * zoomScale}px`,
                    top: `${(forwardHighlight.v - Math.max(forwardHighlight.height, MIN_HIGHLIGHT_HEIGHT_PT)) * zoomScale}px`,
                    width: `${Math.max(forwardHighlight.width, MIN_HIGHLIGHT_WIDTH_PT) * zoomScale}px`,
                    height: `${Math.max(forwardHighlight.height, MIN_HIGHLIGHT_HEIGHT_PT) * zoomScale}px`,
                  }}
                />
              )}
            </div>
          ))}
        </div>
      )}
      {statsOpen && pdfUrl && (
        <div
          className="lemma-pdf-stats-panel"
          id="pdf-statistics-panel"
          role="region"
          aria-label="PDF statistics"
          data-testid="pdf-statistics-panel"
        >
          <dl className="lemma-pdf-stats-list">
            <div className="lemma-pdf-stats-row">
              <dt className="lemma-pdf-stats-label">Pages</dt>
              <dd className="lemma-pdf-stats-value">{formatStatCount(pageCount)}</dd>
            </div>
            <div className="lemma-pdf-stats-row">
              <dt className="lemma-pdf-stats-label">Words</dt>
              <dd className="lemma-pdf-stats-value">{pdfStatCell(statsState, textStats, textStats?.words ?? 0)}</dd>
            </div>
            <div className="lemma-pdf-stats-row">
              <dt className="lemma-pdf-stats-label">Lines</dt>
              <dd className="lemma-pdf-stats-value">{pdfStatCell(statsState, textStats, textStats?.lines ?? 0)}</dd>
            </div>
            <div className="lemma-pdf-stats-row">
              <dt className="lemma-pdf-stats-label">Characters</dt>
              <dd className="lemma-pdf-stats-value">
                {pdfStatCell(statsState, textStats, textStats?.characters ?? 0)}
              </dd>
            </div>
            <div className="lemma-pdf-stats-row">
              <dt className="lemma-pdf-stats-label">Characters excluding spaces</dt>
              <dd className="lemma-pdf-stats-value">
                {pdfStatCell(statsState, textStats, textStats?.charactersExcludingSpaces ?? 0)}
              </dd>
            </div>
          </dl>
        </div>
      )}
      <div className="lemma-pdf-statusbar">
        {pdfUrl ? (
          <button
            type="button"
            className="lemma-pdf-stats-toggle"
            data-testid="pdf-statistics"
            aria-label={`PDF statistics: ${statisticsSummary}`}
            aria-expanded={statsOpen}
            aria-controls="pdf-statistics-panel"
            onClick={() => setStatsOpen((open) => !open)}
          >
            <span className="lemma-pdf-page-indicator">
              {statisticsSummary}
            </span>
            <span className="lemma-status-caret" aria-hidden="true">▾</span>
          </button>
        ) : (
          <span className="lemma-pdf-page-indicator" data-testid="pdf-statistics">
            No PDF
          </span>
        )}
        {pdfUrl ? (
          <a
            href={pdfUrl}
            download={`${projectId}.pdf`}
            className="lemma-btn lemma-btn-secondary lemma-btn-xs"
            aria-label="Download PDF"
          >
            Download PDF
          </a>
        ) : (
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary lemma-btn-xs"
            aria-label="Download PDF"
            aria-disabled="true"
            title="Compile first"
            disabled
          >
            Download PDF
          </button>
        )}
      </div>
    </div>
  );
}

export default PdfPreview;
