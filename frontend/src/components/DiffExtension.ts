import { Facet, StateEffect, StateField, Transaction } from "@codemirror/state";
import { Decoration, EditorView, WidgetType } from "@codemirror/view";

export interface OverlapWarningItem {
  matched_text?: string;
  word_count?: number;
  cite_key?: string | null;
  page_number?: number | null;
}

export interface ClaimEvidenceItem {
  claim_text?: string;
  cite_key?: string;
  grounding_tier?: "full_text_pdf" | "abstract_only";
  snippet?: string;
  page_number?: number | null;
}

export interface IntegrityFlagsItem {
  has_citations?: boolean;
  has_unknown_citations?: boolean;
  has_overlap_warnings?: boolean;
  substantive_prose?: boolean;
  proportionality_nudge?: boolean;
}

export interface IntegrityAssessmentItem {
  tier: 1 | 2 | 3;
  category: string;
  reasons: string[];
  flags: IntegrityFlagsItem;
  suggested_purpose: string;
  section_hint?: string;
}

export interface ProposedHunkItem {
  hunk_id: string;
  file_path: string;
  base_etag: string;
  start_line: number;
  end_line: number;
  original_text: string;
  replacement_text: string;
  cite_keys: string[];
  unknown_cite_keys: string[];
  overlap_warnings: OverlapWarningItem[];
  claim_evidence: ClaimEvidenceItem[];
  injection_warning: boolean;
  integrity?: IntegrityAssessmentItem | null;
  source_surface?: "chat" | "quick_action" | "review_studio" | "manual";
  model_id?: string;
  model_display_name?: string;
  provider?: string;
}

export const setDiffHunksEffect = StateEffect.define();
export const removeDiffHunkEffect = StateEffect.define();

export const diffHunksField = StateField.define({
  create() {
    return [] as ProposedHunkItem[];
  },
  update(hunks: ProposedHunkItem[], tr: Transaction) {
    let next = hunks;
    for (const effect of tr.effects || []) {
      if (effect.is(setDiffHunksEffect)) {
        next = Array.isArray(effect.value) ? [...effect.value] : [];
      } else if (effect.is(removeDiffHunkEffect)) {
        const targetId = String(effect.value);
        next = next.filter((h) => h.hunk_id !== targetId);
      }
    }
    return next;
  },
});

export interface HunkAnchorResolution {
  found: boolean;
  from: number;
  to: number;
  startLine: number;
  endLine: number;
  reanchored: boolean;
}

export function resolveHunkAnchor(
  docText: string,
  hunk: ProposedHunkItem
): HunkAnchorResolution {
  const lines = docText.split("\n");
  const sIdx = Math.max(0, hunk.start_line - 1);
  const eIdx = Math.min(lines.length - 1, Math.max(sIdx, hunk.end_line - 1));

  // Check if original_text still sits at [start_line .. end_line]
  const sliceAtOriginalLines = lines.slice(sIdx, eIdx + 1).join("\n");
  if (sliceAtOriginalLines === hunk.original_text) {
    let from = 0;
    for (let i = 0; i < sIdx; i++) {
      from += lines[i].length + 1;
    }
    const to = from + sliceAtOriginalLines.length;
    return {
      found: true,
      from,
      to,
      startLine: sIdx + 1,
      endLine: eIdx + 1,
      reanchored: false,
    };
  }

  // Re-anchor by searching for hunk.original_text in the current buffer, prioritizing proximity to start_line
  if (hunk.original_text) {
    let bestIndex = -1;
    let minDistance = Infinity;
    let searchStart = 0;

    while (searchStart < docText.length) {
      const matchIndex = docText.indexOf(hunk.original_text, searchStart);
      if (matchIndex === -1) break;
      const beforeText = docText.slice(0, matchIndex);
      const matchLine = beforeText.split("\n").length;
      const dist = Math.abs(matchLine - hunk.start_line);
      if (dist < minDistance) {
        minDistance = dist;
        bestIndex = matchIndex;
      }
      searchStart = matchIndex + 1;
    }

    if (bestIndex !== -1) {
      const beforeText = docText.slice(0, bestIndex);
      const newStartLine = beforeText.split("\n").length;
      const newEndLine =
        newStartLine + hunk.original_text.split("\n").length - 1;
      return {
        found: true,
        from: bestIndex,
        to: bestIndex + hunk.original_text.length,
        startLine: newStartLine,
        endLine: newEndLine,
        reanchored: newStartLine !== hunk.start_line,
      };
    }
  }

  return {
    found: false,
    from: 0,
    to: 0,
    startLine: hunk.start_line,
    endLine: hunk.end_line,
    reanchored: false,
  };
}

export function applyCitationSelectionAndRemapping(
  replacementText: string,
  rejectedCiteKeys: string[] = [],
  keyRemapping: Record<string, string> = {}
): string {
  const rejectedSet = new Set(rejectedCiteKeys);
  const citeCmdRegex =
    /(\\[a-zA-Z]*cite[a-zA-Z]*\*?(?:\s*\[[^\]]*\])*)\{([^}]+)\}/gi;

  return replacementText.replace(
    citeCmdRegex,
    (_fullMatch: string, prefix: string, keysStr: string) => {
      const rawKeys = keysStr
        .split(",")
        .map((k: string) => k.trim())
        .filter(Boolean);
      const surviving = rawKeys
        .filter((k: string) => !rejectedSet.has(k))
        .map((k: string) => keyRemapping[k] || k);
      if (surviving.length === 0) {
        return "";
      }
      return `${prefix}{${surviving.join(", ")}}`;
    }
  );
}

export interface RenderedDiffWidgetDOM {
  tagName: string;
  className: string;
  hunkId: string;
  deletedStyle: { backgroundColor: string; color: string };
  addedStyle: { backgroundColor: string; color: string };
  badges: string[];
  citationCheckboxes: Array<{ citeKey: string; checked: boolean }>;
  acceptBlocked: boolean;
  textContent: string;
}

export interface HunkAcceptChoice {
  allowUnknownCitations: boolean;
  rejectedCiteKeys: string[];
}

export interface DiffHunkActions {
  // Resolves to an error message when the edit was not applied.
  onAccept?: (
    view: EditorView,
    hunk: ProposedHunkItem,
    choice: HunkAcceptChoice
  ) => Promise<string | null>;
  onReject?: (view: EditorView, hunk: ProposedHunkItem) => void;
}

export const diffHunkActions = Facet.define<DiffHunkActions, DiffHunkActions>({
  combine: (values) => values[0] ?? {},
});

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className: string,
  text?: string
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export class InlineDiffHunkWidget extends WidgetType {
  hunk: ProposedHunkItem;
  overrideUnknownCitations: boolean;
  anchored: boolean;

  constructor(hunk: ProposedHunkItem, overrideUnknownCitations = false, anchored = true) {
    super();
    this.hunk = hunk;
    this.overrideUnknownCitations = overrideUnknownCitations;
    this.anchored = anchored;
  }

  eq(other: InlineDiffHunkWidget): boolean {
    return (
      other.hunk.hunk_id === this.hunk.hunk_id &&
      other.overrideUnknownCitations === this.overrideUnknownCitations &&
      other.anchored === this.anchored &&
      JSON.stringify(other.hunk.integrity ?? null) === JSON.stringify(this.hunk.integrity ?? null)
    );
  }

  ignoreEvent(): boolean {
    return true;
  }

  toDOM(view?: EditorView): HTMLElement {
    const badges: string[] = [];
    if (this.hunk.unknown_cite_keys && this.hunk.unknown_cite_keys.length > 0) {
      badges.push(
        `Unknown Citation Key(s): ${this.hunk.unknown_cite_keys.join(", ")}`
      );
    }
    if (this.hunk.overlap_warnings && this.hunk.overlap_warnings.length > 0) {
      badges.push(
        `Copy-overlap check: ${this.hunk.overlap_warnings.length} verbatim overlap warning(s)`
      );
    }
    if (this.hunk.injection_warning) {
      badges.push(
        "Prompt-Injection Warning: Untrusted source excerpt contained instruction-override pattern"
      );
    }
    const integrity = this.hunk.integrity;
    if (integrity && integrity.tier >= 2) {
      if (integrity.flags?.has_citations || integrity.tier === 3) {
        badges.push(
          "Compliance Check (Tier 3): Verify all citations and factual claims against primary sources before compiling."
        );
      }
      if (integrity.flags?.substantive_prose || integrity.tier === 2) {
        badges.push(
          "Disclosure Notice (Tier 2): Substantive content/phrasing change — will auto-log to AI Usage Appendix on accept."
        );
      }
      if (integrity.flags?.proportionality_nudge) {
        badges.push(
          "Proportionality Check: Large generated passage — review critically and ensure human authorship."
        );
      }
    }

    const citationCheckboxes =
      this.hunk.cite_keys && this.hunk.cite_keys.length > 1
        ? this.hunk.cite_keys.map((key) => ({
            citeKey: key,
            checked: !this.hunk.unknown_cite_keys.includes(key),
          }))
        : [];

    const acceptBlocked =
      this.hunk.unknown_cite_keys.length > 0 && !this.overrideUnknownCitations;

    const textParts = [
      `Hunk ${this.hunk.hunk_id} (${this.hunk.file_path}:${this.hunk.start_line}-${this.hunk.end_line})`,
      `- ${this.hunk.original_text}`,
      `+ ${this.hunk.replacement_text}`,
      ...badges,
      ...citationCheckboxes.map((c) => `[${c.checked ? "x" : " "}] \\cite{${c.citeKey}}`),
      acceptBlocked ? "[Accept Blocked: Unknown Citation]" : "[Accept]",
      "[Reject]",
    ];

    if (typeof document !== "undefined" && typeof document.createElement === "function") {
      return this.buildCard(view, badges, citationCheckboxes, acceptBlocked);
    }

    // Fallback for node test environments where document might be mock-provided
    const mockNode: any = {
      tagName: "DIV",
      className: "lemma-cm-inline-diff-widget",
      hunkId: this.hunk.hunk_id,
      deletedStyle: { backgroundColor: "#3f1d24", color: "#f8b4b4" },
      addedStyle: { backgroundColor: "#143828", color: "#a7f3d0" },
      badges,
      citationCheckboxes,
      acceptBlocked,
      textContent: textParts.join("\n"),
    };
    return mockNode as HTMLElement;
  }

  private buildCard(
    view: EditorView | undefined,
    badges: string[],
    citationCheckboxes: Array<{ citeKey: string; checked: boolean }>,
    acceptBlocked: boolean
  ): HTMLElement {
    const hunk = this.hunk;
    const card = el("div", "lemma-cm-diff-card");
    card.setAttribute("data-hunk-id", hunk.hunk_id);
    (card as any).acceptBlocked = acceptBlocked;

    const header = el("div", "lemma-cm-diff-header");
    const titleBlock = el("div", "lemma-cm-diff-title-block");
    titleBlock.appendChild(el("span", "lemma-cm-diff-title", "Proposed edit"));
    const lineRange =
      hunk.start_line === hunk.end_line
        ? `line ${hunk.start_line}`
        : `lines ${hunk.start_line}–${hunk.end_line}`;
    titleBlock.appendChild(
      el(
        "span",
        "lemma-cm-diff-meta",
        this.anchored ? `${hunk.file_path} · ${lineRange}` : `${hunk.file_path} · original text not found`
      )
    );
    // Actions come first: the card is as wide as the longest line, so its right end can be scrolled out of view.
    const actions = el("div", "lemma-cm-diff-actions");
    const acceptBtn = el("button", "lemma-btn lemma-btn-primary lemma-btn-xs", "Accept");
    acceptBtn.type = "button";
    acceptBtn.setAttribute("aria-label", "Accept proposed edit");
    const rejectBtn = el("button", "lemma-btn lemma-btn-secondary lemma-btn-xs", "Reject");
    rejectBtn.type = "button";
    rejectBtn.setAttribute("aria-label", "Reject proposed edit");
    actions.appendChild(acceptBtn);
    actions.appendChild(rejectBtn);
    header.appendChild(actions);
    header.appendChild(titleBlock);
    card.appendChild(header);

    if (!this.anchored) {
      card.appendChild(
        el(
          "p",
          "lemma-cm-diff-note",
          "The text this edit replaces has changed, so it can no longer be placed. Reject it and ask again."
        )
      );
    }

    if (badges.length > 0) {
      const badgeRow = el("div", "lemma-cm-diff-badges");
      if (hunk.unknown_cite_keys.length > 0) {
        badgeRow.appendChild(
          el("span", "lemma-badge-danger", `Unknown Citation Key(s): ${hunk.unknown_cite_keys.join(", ")}`)
        );
      }
      if (hunk.overlap_warnings.length > 0) {
        badgeRow.appendChild(
          el(
            "span",
            "lemma-badge-warning",
            `Copy-overlap check: ${hunk.overlap_warnings.length} verbatim overlap warning(s)`
          )
        );
      }
      if (hunk.injection_warning) {
        badgeRow.appendChild(
          el(
            "span",
            "lemma-badge-danger",
            "Prompt-Injection Warning: Untrusted source excerpt contained instruction-override pattern"
          )
        );
      }
      card.appendChild(badgeRow);
    }

    const citeInputs: Array<{ key: string; input: HTMLInputElement }> = [];
    if (citationCheckboxes.length > 0) {
      const citeRow = el("div", "lemma-cm-diff-cites");
      citeRow.appendChild(el("span", "lemma-cm-diff-cites-label", "Keep citations:"));
      for (const c of citationCheckboxes) {
        const label = el("label", "lemma-checkbox-label lemma-cm-diff-cite");
        const input = el("input", "");
        input.type = "checkbox";
        input.checked = c.checked;
        label.appendChild(input);
        label.appendChild(el("code", "", c.citeKey));
        citeRow.appendChild(label);
        citeInputs.push({ key: c.citeKey, input });
      }
      card.appendChild(citeRow);
    }

    let overrideInput: HTMLInputElement | null = null;
    if (hunk.unknown_cite_keys.length > 0) {
      const label = el("label", "lemma-checkbox-label lemma-cm-diff-override");
      overrideInput = el("input", "");
      overrideInput.type = "checkbox";
      overrideInput.checked = this.overrideUnknownCitations;
      label.appendChild(overrideInput);
      label.appendChild(document.createTextNode("Accept despite unknown citation keys"));
      card.appendChild(label);
    }

    const errorLine = el("p", "lemma-cm-diff-error");
    errorLine.setAttribute("role", "alert");
    errorLine.hidden = true;
    card.appendChild(errorLine);

    const syncAcceptState = () => {
      const blocked = !this.anchored || (hunk.unknown_cite_keys.length > 0 && !overrideInput?.checked);
      acceptBtn.disabled = blocked;
      acceptBtn.title = !this.anchored
        ? "The original text was not found"
        : blocked
          ? "Blocked: unknown citation keys"
          : "";
    };
    syncAcceptState();
    overrideInput?.addEventListener("change", syncAcceptState);

    rejectBtn.addEventListener("click", () => {
      if (!view) return;
      const handler = view.state.facet(diffHunkActions).onReject;
      if (handler) handler(view, hunk);
      else rejectProposedHunk(view, hunk.hunk_id);
    });

    acceptBtn.addEventListener("click", async () => {
      const handler = view?.state.facet(diffHunkActions).onAccept;
      if (!view || !handler) return;
      acceptBtn.disabled = true;
      rejectBtn.disabled = true;
      acceptBtn.textContent = "Applying…";
      errorLine.hidden = true;
      let error: string | null;
      try {
        error = await handler(view, hunk, {
          allowUnknownCitations: Boolean(overrideInput?.checked),
          rejectedCiteKeys: citeInputs.filter((c) => !c.input.checked).map((c) => c.key),
        });
      } catch (err: any) {
        error = err?.message || "Could not apply this edit.";
      }
      if (!card.isConnected) return;
      acceptBtn.textContent = "Accept";
      rejectBtn.disabled = false;
      syncAcceptState();
      if (error) {
        errorLine.textContent = error;
        errorLine.hidden = false;
      }
    });

    return card;
  }
}

class DiffReplacementWidget extends WidgetType {
  hunk: ProposedHunkItem;

  constructor(hunk: ProposedHunkItem) {
    super();
    this.hunk = hunk;
  }

  eq(other: DiffReplacementWidget): boolean {
    return other.hunk.hunk_id === this.hunk.hunk_id;
  }

  toDOM(): HTMLElement {
    const block = el("div", "lemma-cm-diff-added");
    if (!this.hunk.replacement_text) {
      block.classList.add("is-deletion");
      block.textContent = "The highlighted lines will be deleted.";
      return block;
    }
    for (const line of this.hunk.replacement_text.split("\n")) {
      block.appendChild(el("div", "lemma-cm-diff-added-line", line || "\u00a0"));
    }
    return block;
  }
}

const removedLineDecoration = Decoration.line({ class: "lemma-cm-diff-removed-line" });

function buildDiffDecorations(state: any) {
  const hunks: ProposedHunkItem[] =
    typeof state?.field === "function" ? state.field(diffHunksField, false) ?? [] : [];
  if (hunks.length === 0) return Decoration.none;
  const doc = state.doc;
  const docText: string = doc.toString();
  const ranges: any[] = [];
  for (const h of hunks) {
    const anchor = resolveHunkAnchor(docText, h);
    if (!anchor.found) {
      ranges.push(
        Decoration.widget({ widget: new InlineDiffHunkWidget(h, false, false), block: true, side: -1 }).range(0)
      );
      continue;
    }
    const firstLine = doc.lineAt(anchor.from);
    const lastLine = doc.lineAt(anchor.to);
    ranges.push(
      Decoration.widget({ widget: new InlineDiffHunkWidget(h), block: true, side: -1 }).range(firstLine.from)
    );
    if (anchor.to > anchor.from) {
      for (let n = firstLine.number; n <= lastLine.number; n++) {
        ranges.push(removedLineDecoration.range(doc.line(n).from));
      }
    }
    ranges.push(
      Decoration.widget({ widget: new DiffReplacementWidget(h), block: true, side: 1 }).range(lastLine.to)
    );
  }
  return Decoration.set(ranges, true);
}

export interface AcceptHunkOptions {
  view: EditorView;
  currentEtag: string | null;
  hunk: ProposedHunkItem;
  allowUnknownCitations?: boolean;
  rejectedCiteKeys?: string[];
  keyRemapping?: Record<string, string>;
  projectId?: string;
}

export interface AcceptHunkResult {
  accepted: boolean;
  reanchored: boolean;
  conflict?: boolean;
  blockedReason?: string;
  newEtag?: string | null;
  appliedText?: string;
}

export async function acceptProposedHunk(
  options: AcceptHunkOptions
): Promise<AcceptHunkResult> {
  const {
    view,
    currentEtag,
    hunk,
    allowUnknownCitations = false,
    rejectedCiteKeys = [],
    keyRemapping = {},
    projectId,
  } = options;

  if (hunk.unknown_cite_keys.length > 0 && !allowUnknownCitations) {
    return {
      accepted: false,
      reanchored: false,
      blockedReason: `Blocked due to unknown citation key(s): ${hunk.unknown_cite_keys.join(", ")}`,
    };
  }

  const currentDoc = view.state.doc.toString();
  const anchor = resolveHunkAnchor(currentDoc, hunk);
  if (!anchor.found) {
    return {
      accepted: false,
      reanchored: false,
      conflict: true,
      blockedReason:
        "Original text was modified in the buffer; cannot anchor diff hunk safely.",
    };
  }

  const finalReplacement = applyCitationSelectionAndRemapping(
    hunk.replacement_text,
    rejectedCiteKeys,
    keyRemapping
  );

  const updatedDoc =
    currentDoc.slice(0, anchor.from) +
    finalReplacement +
    currentDoc.slice(anchor.to);

  let newEtag = currentEtag;

  if (projectId && typeof fetch === "function") {
    const csrfToken =
      typeof localStorage !== "undefined"
        ? localStorage.getItem("lemma_csrf_token") || ""
        : "";
    const res = await fetch(
      `/api/projects/${projectId}/files/${hunk.file_path}`,
      {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          content: updatedDoc,
          expected_etag: currentEtag || hunk.base_etag,
        }),
      }
    );
    if (res.status === 409) {
      return {
        accepted: false,
        reanchored: anchor.reanchored,
        conflict: true,
        blockedReason: "ETag conflict on disk while saving accepted hunk.",
      };
    }
    if (res.ok) {
      const data = await res.json();
      if (data && data.etag) {
        newEtag = data.etag;
      }
    } else {
      return {
        accepted: false,
        reanchored: anchor.reanchored,
        blockedReason: `Failed to save file: HTTP ${res.status}`,
      };
    }
  }

  // Only dispatch buffer mutation and remove hunk after PUT succeeds
  view.dispatch({
    changes: {
      from: anchor.from,
      to: anchor.to,
      insert: finalReplacement,
    },
    effects: removeDiffHunkEffect.of(hunk.hunk_id),
  });

  return {
    accepted: true,
    reanchored: anchor.reanchored,
    newEtag,
    appliedText: finalReplacement,
  };
}

export function rejectProposedHunk(view: EditorView, hunkId: string): void {
  view.dispatch({
    effects: removeDiffHunkEffect.of(hunkId),
  });
}

export function inlineDiffExtension(
  initialHunks: ProposedHunkItem[] = [],
  actions?: DiffHunkActions
) {
  const exts: any[] = [
    typeof (diffHunksField as any).init === "function"
      ? (diffHunksField as any).init(() => initialHunks)
      : diffHunksField,
  ];
  if (actions && typeof (diffHunkActions as any).of === "function") {
    exts.push(diffHunkActions.of(actions));
  }

  if (typeof (EditorView.decorations as any).compute === "function") {
    exts.push(
      (EditorView.decorations as any).compute([diffHunksField, "doc"], buildDiffDecorations)
    );
  }

  return exts;
}
