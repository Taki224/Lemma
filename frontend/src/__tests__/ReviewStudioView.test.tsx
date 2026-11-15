import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ReviewStudioView, type ReviewIssueItem } from "../components/ReviewStudioView";
import { EnabledModelsContext, type EnabledModelInfo } from "../context/EnabledModelsContext";

function makeModel(id: string, supportsVision = false): EnabledModelInfo {
  return {
    id,
    name: `Name of ${id}`,
    supports_vision: supportsVision,
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

function optionIds(select: any): string[] {
  return select.querySelectorAll("option").map((o: any) => String(o.props.value));
}

async function waitForModelsReady(): Promise<void> {
  await waitFor(() => {
    expect(screen.getByTestId("run-review-btn").props.disabled).toBe(false);
  });
}

function mockReviewFetch(): any {
  const fn = vi.fn().mockImplementation((url: string, options?: any) => {
    const urlStr = String(url);
    if (urlStr.endsWith("/reviews/estimate") && options?.method === "POST") {
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({
            page_count: 3,
            estimated_prompt_tokens: 100,
            estimated_completion_tokens: 50,
            estimated_usd: 0.1,
            requires_confirmation: false,
          }),
      });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ reports: [] }) });
  });
  global.fetch = fn;
  return fn;
}

describe("ReviewStudioView", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("test_prompts_cost_confirmation_sanitizes_markdown_and_jumps_to_line_or_page", async () => {
    const onJumpToLine = vi.fn();
    const onJumpToPage = vi.fn();

    const sampleIssues: ReviewIssueItem[] = [
      {
        issue_id: "iss_1",
        reviewer_persona: "methodology_critic",
        model: "anthropic/claude-3.7-sonnet",
        severity: "major",
        file_path: "sections/intro.tex",
        original_line: 15,
        resolved_line: 25,
        page_number: 2,
        quoted_anchor: "Our algorithm outperforms baseline by 40%.",
        title: "Missing statistical significance test",
        critique: "The claim is unsupported by confidence intervals. ![Remote tracker](https://evil.example/track.png)",
        suggested_fix: "Report p-values and 95% confidence intervals.",
        stale: false,
      },
      {
        issue_id: "iss_2",
        reviewer_persona: "literature_auditor",
        model: "google/gemini-2.5-pro",
        severity: "minor",
        file_path: "main.tex",
        original_line: 5,
        resolved_line: 5,
        page_number: 1,
        quoted_anchor: "Old deleted quote",
        title: "Prior work not discussed",
        critique: "Missing reference to Smith et al. (2024).",
        stale: true,
      },
      {
        issue_id: "iss_3",
        reviewer_persona: "visual_layout_inspector",
        model: "anthropic/claude-3.7-sonnet",
        severity: "nit",
        file_path: "",
        page_number: 4,
        quoted_anchor: "",
        title: "Equation overfull into gutter",
        critique: "Equation (12) protrudes 12pt into right column.",
        suggested_fix: "Wrap in multline environment.",
        stale: false,
      },
    ];

    global.fetch = vi.fn().mockImplementation((url: string, options?: any) => {
      const urlStr = String(url);
      if (urlStr.endsWith("/reviews") && (!options || options.method === "GET")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ reports: [] }),
        });
      }
      if (urlStr.endsWith("/reviews/estimate") && options?.method === "POST") {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              page_count: 8,
              estimated_prompt_tokens: 4000,
              estimated_completion_tokens: 1500,
              estimated_usd: 0.75,
              requires_confirmation: true,
            }),
        });
      }
      if (urlStr.endsWith("/reviews/run") && options?.method === "POST") {
        const body = JSON.parse(options.body || "{}");
        if (!body.confirm_high_cost) {
          return Promise.resolve({
            ok: false,
            status: 400,
            json: () => Promise.resolve({ detail: "Review estimated cost requires confirmation" }),
          });
        }
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              report_id: "review-20261101-120000",
              snapshot_sha: "commit-sha-1234",
              issues: sampleIssues,
              failed_reviewers: [],
            }),
        });
      }
      return Promise.reject(new Error(`Unhandled fetch: ${urlStr}`));
    });

    render(
      withModels(
        [makeModel("a/model", true), makeModel("b/model")],
        <ReviewStudioView
          projectId="proj_test"
          csrfToken="csrf-secret-token"
          onJumpToLine={onJumpToLine}
          onJumpToPage={onJumpToPage}
        />
      )
    );

    await waitForModelsReady();

    // Verify persona model options exist
    expect(Boolean(screen.getByTestId("methodology-model-select"))).toBe(true);
    expect(Boolean(screen.getByTestId("literature-model-select"))).toBe(true);

    // 1. Click Estimate Cost
    const estimateBtn = screen.getByTestId("estimate-cost-btn");
    fireEvent.click(estimateBtn);

    await waitFor(() => {
      expect(Boolean(screen.getByTestId("estimate-usd-badge"))).toBe(true);
    });
    expect(screen.getByTestId("estimate-usd-badge").textContent).toContain("0.75");
    expect(Boolean(screen.getByTestId("requires-confirm-badge"))).toBe(true);

    // 2. Click Run Review -> Confirmation modal should appear because requires_confirmation is true
    const runBtn = screen.getByTestId("run-review-btn");
    fireEvent.click(runBtn);

    await waitFor(() => {
      expect(Boolean(screen.getByTestId("cost-confirmation-modal"))).toBe(true);
    });
    expect(screen.getByTestId("cost-confirmation-modal").textContent).toContain("0.75");

    // 3. Click Confirm in the modal
    const confirmBtn = screen.getByTestId("confirm-high-cost-btn");
    fireEvent.click(confirmBtn);

    // 4. Issues should load and render
    await waitFor(() => {
      const cards = screen.getAllByTestId("review-issue-card");
      expect(cards.length).toBe(3);
    });

    // 5. Verify SanitizedMarkdown rendered remote image as blocked text, not <img> tag
    const majorCard = screen.getAllByTestId("review-issue-card")[0];
    expect(majorCard.textContent).toContain("Remote image blocked");
    expect(majorCard.querySelector("img")).toBeNull();

    // 6. Verify Stale badge is present on issue 2
    const staleBadges = screen.getAllByTestId("stale-badge");
    expect(staleBadges.length).toBe(1);

    // 7. Verify Jump to line callback
    const jumpLineBtn = screen.getByTestId("jump-line-btn-iss_1");
    fireEvent.click(jumpLineBtn);
    expect(onJumpToLine).toHaveBeenCalledWith("sections/intro.tex", 25);

    // 8. Verify Jump to page callback
    const jumpPageBtn = screen.getByTestId("jump-page-btn-iss_3");
    fireEvent.click(jumpPageBtn);
    expect(onJumpToPage).toHaveBeenCalledWith(4);
  });

  it("vision select lists only vision-capable models", () => {
    mockReviewFetch();
    render(
      withModels(
        [makeModel("a/text"), makeModel("b/vision", true)],
        <ReviewStudioView projectId="proj_test" csrfToken="tok" />
      )
    );

    expect(optionIds(screen.getByTestId("vision-model-select"))).toEqual(["b/vision"]);
    expect(optionIds(screen.getByTestId("methodology-model-select"))).toEqual([
      "a/text",
      "b/vision",
    ]);
  });

  it("sends the enabled default models in the estimate request", async () => {
    const fetchMock = mockReviewFetch();
    render(
      withModels(
        [makeModel("a/text"), makeModel("b/vision", true)],
        <ReviewStudioView projectId="proj_test" csrfToken="tok" />
      )
    );

    await waitForModelsReady();
    fireEvent.click(screen.getByTestId("estimate-cost-btn"));

    await waitFor(() => {
      expect(Boolean(screen.getByTestId("estimate-usd-badge"))).toBe(true);
    });
    const call = fetchMock.mock.calls.find((c: any[]) => String(c[0]).endsWith("/reviews/estimate"));
    const body = JSON.parse(call[1].body);
    expect(body.assignments.map((a: any) => a.model)).toEqual(["a/text", "a/text", "a/text"]);
    expect(body.include_visual).toBe(true);
    expect(body.vision_model).toBe("b/vision");
  });

  it("visual option disabled when no vision model enabled", async () => {
    const fetchMock = mockReviewFetch();
    render(
      withModels(
        [makeModel("a/text")],
        <ReviewStudioView projectId="proj_test" csrfToken="tok" />
      )
    );

    expect(screen.getByTestId("include-visual-checkbox").props.disabled).toBe(true);
    expect(screen.getByTestId("vision-model-select").props.disabled).toBe(true);
    expect(Boolean(screen.getByText("No vision-capable model enabled — open Settings > Models"))).toBe(true);

    await waitForModelsReady();
    fireEvent.click(screen.getByTestId("estimate-cost-btn"));

    await waitFor(() => {
      expect(Boolean(screen.getByTestId("estimate-usd-badge"))).toBe(true);
    });
    const call = fetchMock.mock.calls.find((c: any[]) => String(c[0]).endsWith("/reviews/estimate"));
    const body = JSON.parse(call[1].body);
    expect(body.include_visual).toBe(false);
    expect(body.vision_model).toBeNull();
  });

  it("run disabled when no models enabled", async () => {
    const fetchMock = mockReviewFetch();
    render(
      withModels([], <ReviewStudioView projectId="proj_test" csrfToken="tok" />)
    );

    expect(screen.getByTestId("run-review-btn").props.disabled).toBe(true);
    expect(screen.getByTestId("estimate-cost-btn").props.disabled).toBe(true);

    fireEvent.click(screen.getByTestId("run-review-btn"));
    fireEvent.click(screen.getByTestId("estimate-cost-btn"));
    await new Promise((r) => setTimeout(r, 30));

    const posts = fetchMock.mock.calls.filter((c: any[]) => c[1]?.method === "POST");
    expect(posts.length).toBe(0);
  });
});
