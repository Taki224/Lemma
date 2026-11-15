import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  SelectionQuickActionBar,
  ToolsMenuModal,
  ExportModal,
} from "../components/ToolsMenuAndToolbar";
import { EnabledModelsContext, type EnabledModelInfo } from "../context/EnabledModelsContext";

function makeModel(id: string): EnabledModelInfo {
  return {
    id,
    name: `Name of ${id}`,
    supports_vision: false,
    verified: true,
    prompt_price_per_token: 0.000003,
    completion_price_per_token: 0.000015,
  };
}

function withModels(models: EnabledModelInfo[], ui: React.ReactElement): React.ReactElement {
  return (
    <EnabledModelsContext.Provider value={{ models, loading: false, refresh: async () => {} }}>
      {ui}
    </EnabledModelsContext.Provider>
  );
}

const NO_MODEL_TOOLTIP = "Enable a model in Settings > Models";

describe("ToolsMenuAndToolbar", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("test_selection_toolbar_triggers_quick_action_and_export_modal_shows_verification_status", async () => {
    const onHunkProposed = vi.fn();
    const onJumpToLine = vi.fn();

    const mockHunk = {
      hunk_id: "hunk_tighten_1",
      file_path: "main.tex",
      base_etag: "etag123",
      start_line: 10,
      end_line: 12,
      original_text: "Wordy text that can be tightened.",
      replacement_text: "Concise text.",
      cite_keys: ["smith2020"],
      unknown_cite_keys: [],
      overlap_warnings: [],
      claim_evidence: [],
      injection_warning: false,
    };

    global.fetch = vi.fn().mockImplementation((url: string, options?: any) => {
      const urlStr = String(url);
      if (urlStr.includes("/tools/quick-action")) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              action: "tighten",
              hunk: mockHunk,
              injection_warning: false,
            }),
        });
      }
      if (urlStr.includes("/tools/visual-to-latex")) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              latex_code: "\\begin{table}...\n\\end{table}",
              target_format: "booktabs",
              is_safe: true,
              lint_warnings: [],
              hunk: {
                ...mockHunk,
                hunk_id: "hunk_table_1",
                replacement_text: "\\begin{table}...\n\\end{table}",
              },
            }),
        });
      }
      if (urlStr.includes("/export")) {
        return Promise.resolve({
          ok: true,
          headers: new Headers({
            "content-type": "application/zip",
            "x-verification-passed": "true",
            "x-included-files": JSON.stringify(["main.tex", "references.bib", "main.bbl"]),
            "x-stripped-files": JSON.stringify(["unused.png", "paper-reviews/r1.md"]),
          }),
          blob: () => Promise.resolve(new Blob(["dummy-zip-bytes"], { type: "application/zip" })),
        });
      }
      return Promise.reject(new Error(`Unhandled fetch: ${urlStr}`));
    });

    // 1. Verify SelectionQuickActionBar triggers quick action and routes to inline diff
    const { unmount: unmountBar } = render(
      withModels(
        [makeModel("a/model")],
        <SelectionQuickActionBar
          projectId="proj_test"
          filePath="main.tex"
          selectedText="Wordy text that can be tightened."
          startLine={10}
          endLine={12}
          csrfToken="csrf_123"
          onHunkProposed={onHunkProposed}
        />
      )
    );

    const tightenBtn = screen.getByRole("button", { name: /tighten/i });
    fireEvent.click(tightenBtn);

    await waitFor(() => {
      expect(onHunkProposed.mock.calls.length).toBeGreaterThan(0);
      expect(onHunkProposed.mock.calls[0][0].hunk_id).toBe("hunk_tighten_1");
      expect(onHunkProposed.mock.calls[0][0].replacement_text).toBe("Concise text.");
    });

    unmountBar();

    // 2. Verify Visual-to-LaTeX converter inside ToolsMenuModal routes hunk to inline diff
    const { unmount: unmountModal } = render(
      <ToolsMenuModal
        projectId="proj_test"
        csrfToken="csrf_123"
        initialTab="visual_to_latex"
        isOpen={true}
        onClose={vi.fn()}
        onHunkProposed={onHunkProposed}
        onJumpToLine={onJumpToLine}
      />
    );

    const csvTextarea = screen.getByRole("textbox", { name: /csv data/i });
    fireEvent.change(csvTextarea, { target: { value: "A,B\n1,2" } });

    const convertBtn = screen.getByRole("button", { name: /convert to latex/i });
    fireEvent.click(convertBtn);

    await waitFor(() => {
      expect(onHunkProposed.mock.calls.length).toBeGreaterThan(1);
      expect(onHunkProposed.mock.calls[1][0].hunk_id).toBe("hunk_table_1");
    });

    unmountModal();

    // 3. Verify ExportModal shows verification status badge and file lists
    render(
      <ExportModal
        projectId="proj_test"
        csrfToken="csrf_123"
        isOpen={true}
        onClose={vi.fn()}
      />
    );

    const exportBtn = screen.getByRole("button", { name: /export camera-ready/i });
    fireEvent.click(exportBtn);

    await waitFor(() => {
      expect(screen.getByText(/verification passed/i)).toBeDefined();
      expect(screen.getByText("main.tex")).toBeDefined();
      expect(screen.getByText("unused.png")).toBeDefined();
    });
  });

  function renderBar(models: EnabledModelInfo[], model?: string): void {
    render(
      withModels(
        models,
        <SelectionQuickActionBar
          projectId="proj_test"
          filePath="main.tex"
          selectedText="Wordy text."
          startLine={1}
          endLine={2}
          csrfToken="csrf_123"
          model={model}
        />
      )
    );
  }

  function mockQuickActionFetch(): any {
    const fn = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ action: "tighten", injection_warning: false }),
    });
    global.fetch = fn;
    return fn;
  }

  it("quick action uses first enabled model", async () => {
    const fetchMock = mockQuickActionFetch();
    renderBar([makeModel("a/model"), makeModel("b/model")]);

    fireEvent.click(screen.getByRole("button", { name: /tighten/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).model).toBe("a/model");
  });

  it("explicit model prop wins", async () => {
    const fetchMock = mockQuickActionFetch();
    renderBar([makeModel("a/model")], "x/explicit");

    fireEvent.click(screen.getByRole("button", { name: /tighten/i }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).model).toBe("x/explicit");
  });

  it("quick action disabled when no model enabled", async () => {
    const fetchMock = mockQuickActionFetch();
    renderBar([]);

    for (const name of [/tighten/i, /verify claim/i, /formal academic tone/i, /explain math/i]) {
      const btn = screen.getByRole("button", { name });
      expect(btn.props.disabled).toBe(true);
      expect(btn.props.title).toBe(NO_MODEL_TOOLTIP);
    }

    fireEvent.click(screen.getByRole("button", { name: /tighten/i }));
    await new Promise((r) => setTimeout(r, 30));
    expect(fetchMock).toHaveBeenCalledTimes(0);
  });
});
