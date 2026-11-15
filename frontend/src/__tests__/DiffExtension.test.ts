import { test, expect, vi } from "vitest";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import {
  type ProposedHunkItem,
  InlineDiffHunkWidget,
  inlineDiffExtension,
  diffHunksField,
  acceptProposedHunk,
  rejectProposedHunk,
} from "../components/DiffExtension";

test("test_accept_hunk_verifies_base_etag_reanchors_shifted_lines_and_reject_discards", async () => {
  const initialDoc = [
    "\\documentclass{article}",
    "\\begin{document}",
    "\\section{Introduction}",
    "Original claim before editing.",
    "\\end{document}",
  ].join("\n");

  const hunk1: ProposedHunkItem = {
    hunk_id: "hunk-1",
    file_path: "main.tex",
    base_etag: "etag-v1",
    start_line: 4,
    end_line: 4,
    original_text: "Original claim before editing.",
    replacement_text: "Revised claim with verified citations \\cite{vaswani2017attention, smith2024}.",
    cite_keys: ["vaswani2017attention", "smith2024"],
    unknown_cite_keys: [],
    overlap_warnings: [],
    claim_evidence: [],
    injection_warning: false,
  };

  const hunk2: ProposedHunkItem = {
    hunk_id: "hunk-2",
    file_path: "main.tex",
    base_etag: "etag-v1",
    start_line: 3,
    end_line: 3,
    original_text: "\\section{Introduction}",
    replacement_text: "\\section{Background and Introduction}",
    cite_keys: [],
    unknown_cite_keys: [],
    overlap_warnings: [],
    claim_evidence: [],
    injection_warning: false,
  };

  const state = EditorState.create({
    doc: initialDoc,
    extensions: [inlineDiffExtension([hunk1, hunk2])],
  });
  const view = new EditorView({ state });

  // Reject hunk2 first -> document text unchanged, hunk2 removed from state field
  rejectProposedHunk(view, "hunk-2");
  expect(view.state.doc.toString()).toBe(initialDoc);
  const remainingAfterReject = view.state.field(diffHunksField);
  expect(remainingAfterReject.map((h: ProposedHunkItem) => h.hunk_id)).toEqual(["hunk-1"]);

  // Simulate user inserting 2 comment lines at top of buffer before accepting hunk1 (lines shift down by 2)
  const shiftedDoc = "% Added comment line 1\n% Added comment line 2\n" + initialDoc;
  view.dispatch({
    changes: { from: 0, to: view.state.doc.length, insert: shiftedDoc },
  });

  const putCalls: Array<{ url: string; body: any }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      putCalls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
      return {
        ok: true,
        status: 200,
        json: async () => ({ etag: "etag-v2", path: "main.tex", size_bytes: 120 }),
      };
    })
  );

  // Accept hunk1 with re-anchoring and key collision remapping (smith2024 -> smith2024a)
  const result = await acceptProposedHunk({
    view,
    currentEtag: "etag-v1",
    hunk: hunk1,
    projectId: "manuscript-proj",
    keyRemapping: { smith2024: "smith2024a" },
  });

  expect(result.accepted).toBe(true);
  expect(result.reanchored).toBe(true);
  const updatedDoc = view.state.doc.toString();
  expect(updatedDoc).toContain("% Added comment line 1");
  expect(updatedDoc).toContain(
    "Revised claim with verified citations \\cite{vaswani2017attention, smith2024a}."
  );
  expect(updatedDoc).not.toContain("Original claim before editing.");
  expect(putCalls.length).toBe(1);
  expect(putCalls[0].body.expected_etag).toBe("etag-v1");
});

test("test_hunk_with_unknown_cite_key_or_injection_warning_renders_badges", async () => {
  const doc = "\\section{Methods}\nBaseline description.\n";
  const riskyHunk: ProposedHunkItem = {
    hunk_id: "hunk-risky",
    file_path: "main.tex",
    base_etag: "etag-v1",
    start_line: 2,
    end_line: 2,
    original_text: "Baseline description.",
    replacement_text: "Manipulated text citing \\cite{hallucinated2099, valid2024}.",
    cite_keys: ["hallucinated2099", "valid2024"],
    unknown_cite_keys: ["hallucinated2099"],
    overlap_warnings: [{ matched_text: "eight consecutive words copied verbatim from source", word_count: 8 }],
    claim_evidence: [],
    injection_warning: true,
  };

  const widget = new InlineDiffHunkWidget(riskyHunk);
  const rendered = widget.toDOM();

  expect(rendered.textContent).toContain("hallucinated2099");
  expect(rendered.textContent).toMatch(/Unknown Citation/i);
  expect(rendered.textContent).toMatch(/Prompt-Injection Warning/i);
  expect(rendered.textContent).toMatch(/Copy-overlap check/i);
  expect(rendered.acceptBlocked).toBe(true);

  // Silent acceptance without override must be blocked when unknown_cite_keys is non-empty
  const state = EditorState.create({
    doc,
    extensions: [inlineDiffExtension([riskyHunk])],
  });
  const view = new EditorView({ state });

  const blockedAttempt = await acceptProposedHunk({
    view,
    currentEtag: "etag-v1",
    hunk: riskyHunk,
    allowUnknownCitations: false,
  });
  expect(blockedAttempt.accepted).toBe(false);
  expect(blockedAttempt.blockedReason).toMatch(/unknown citation/i);
  expect(view.state.doc.toString()).toBe(doc);
});

test("test_inline_diff_extension_initializes_with_factory_function_without_type_error", () => {
  // CodeMirror 6 StateField.init expects a factory function (state: EditorState) => Value.
  // Passing raw array causes TypeError: ((intermediate value) || this.createF) is not a function
  // when EditorState is created (e.g. when opening or creating a project).
  const state = EditorState.create({
    doc: "\\begin{document}\nHello World\n\\end{document}",
    extensions: [inlineDiffExtension()],
  });
  expect(state.field(diffHunksField)).toEqual([]);
});

