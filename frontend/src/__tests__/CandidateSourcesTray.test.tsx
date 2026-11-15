import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import {
  CandidateSourcesTray,
  type CandidateSourceItem,
} from "../components/CandidateSourcesTray";

test("test_renders_abstract_only_badge_and_pdf_upgrade_button", async () => {
  const stagedCandidates: CandidateSourceItem[] = [
    {
      candidate_id: "cand_abs_1",
      project_id: "manuscript-2026",
      cite_key: "smith2024",
      title: "Neural Proof Grounding Without Full PDFs",
      authors: ["Smith, Clara", "Doe, John"],
      year: 2024,
      doi: "10.1000/smith.2024",
      abstract: "Abstract text only.",
      source_api: "semantic_scholar",
      bibtex: "@article{smith2024, title={Neural Proof Grounding Without Full PDFs}}",
      grounding_tier: "abstract_only",
      pdf_path: null,
      created_at: "2026-11-01T00:00:00Z",
      expires_at: "2026-11-15T00:00:00Z",
      status: "staged",
    },
    {
      candidate_id: "cand_pdf_2",
      project_id: "manuscript-2026",
      cite_key: "lewis2020rag",
      title: "Retrieval-Augmented Generation for Knowledge-Intensive NLP",
      authors: ["Lewis, Patrick"],
      year: 2020,
      doi: "10.48550/arXiv.2005.11401",
      abstract: "Full text available.",
      source_api: "arxiv",
      bibtex: "@inproceedings{lewis2020rag, title={Retrieval-Augmented Generation}}",
      grounding_tier: "full_text_pdf",
      pdf_path: "project-uploads/lewis2020rag.pdf",
      created_at: "2026-11-01T00:00:00Z",
      expires_at: "2026-11-15T00:00:00Z",
      status: "staged",
    },
  ];

  const mockFetch = vi.fn(async (url: string, _init?: RequestInit) => {
    if (String(url).includes("/upload-pdf")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          candidate: {
            ...stagedCandidates[0],
            grounding_tier: "full_text_pdf",
            pdf_path: "project-uploads/smith2024.pdf",
          },
        }),
      };
    }
    if (String(url).includes("/candidates/decision")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          accepted_cite_keys: ["smith2024a"],
          rejected_cite_keys: ["lewis2020rag"],
          key_remapping: { smith2024: "smith2024a" },
          zotero_pushed_keys: [],
          bib_etag: "etag-bib-updated",
        }),
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ candidates: stagedCandidates }),
    };
  });

  const onDecisionApplied = vi.fn();

  render(
    <CandidateSourcesTray
      projectId="manuscript-2026"
      initialCandidates={stagedCandidates}
      csrfToken="csrf-tray-test"
      fetchImpl={mockFetch as unknown as typeof fetch}
      onDecisionApplied={onDecisionApplied}
    />
  );

  // Tray is collapsed by default; expand to reveal the items
  fireEvent.click(
    screen.getByRole("button", { name: /Papers the AI suggests/i })
  );

  // 1. Verify exact required grounding tier badges render
  expect(
    screen.getByText(
      "Grounded on Abstract Only — Upload PDF for full-text verification"
    )
  ).toBeInTheDocument();
  expect(screen.getByText("Full-Text PDF Grounded")).toBeInTheDocument();

  // 2. Verify DOI badge and TTL expiry countdown render
  expect(screen.getByText(/10\.1000\/smith\.2024/i)).toBeInTheDocument();
  expect(screen.getAllByText(/Expires in/i).length).toBeGreaterThanOrEqual(1);

  // 3. Verify PDF upgrade button exists for abstract_only candidate and upgrades it to Full-Text PDF Grounded
  const uploadBtn = screen.getByRole("button", {
    name: /Upload PDF for smith2024/i,
  });
  expect(uploadBtn).toBeInTheDocument();
  fireEvent.click(uploadBtn);

  await waitFor(() => {
    expect(
      screen.queryByText(
        "Grounded on Abstract Only — Upload PDF for full-text verification"
      )
    ).toBeNull();
  });

  // 4. Verify per-cite hunk acceptance applies key_remapping (smith2024 -> smith2024a)
  const acceptSmithBtn = screen.getByRole("button", {
    name: /Accept citation smith2024/i,
  });
  fireEvent.click(acceptSmithBtn);

  await waitFor(() => {
    expect(onDecisionApplied).toHaveBeenCalled();
  });
});

const makeCandidate = (cite_key: string): CandidateSourceItem => ({
  candidate_id: `cand_${cite_key}`,
  project_id: "manuscript-2026",
  cite_key,
  title: `Title of ${cite_key}`,
  authors: ["Doe, Jane"],
  year: 2024,
  doi: null,
  abstract: null,
  source_api: "arxiv",
  bibtex: `@article{${cite_key}}`,
  grounding_tier: "abstract_only",
  pdf_path: null,
  created_at: "2026-11-01T00:00:00Z",
  expires_at: "2026-11-15T00:00:00Z",
  status: "staged",
});

const EXPLANATION =
  "Papers found while the AI wrote. Accept to add them to your bibliography, or reject.";

test("test_hidden_when_empty", () => {
  const view = render(
    <CandidateSourcesTray projectId="default" initialCandidates={[]} />
  );
  expect(view.container).toBeNull();
  expect(screen.queryByTestId("candidate-sources-tray")).toBeNull();
  expect(screen.queryByText(/No staged candidate papers/i)).toBeNull();
});

test("test_hidden_after_all_decided", async () => {
  const mockFetch = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      accepted_cite_keys: ["solo2024"],
      rejected_cite_keys: [],
      key_remapping: {},
      zotero_pushed_keys: [],
      bib_etag: null,
    }),
  }));
  render(
    <CandidateSourcesTray
      projectId="manuscript-2026"
      initialCandidates={[makeCandidate("solo2024")]}
      fetchImpl={mockFetch as unknown as typeof fetch}
    />
  );
  fireEvent.click(
    screen.getByRole("button", { name: /Papers the AI suggests/i })
  );
  fireEvent.click(
    screen.getByRole("button", { name: /Accept citation solo2024/i })
  );
  // Nothing left to review, but the outcome of the decision stays visible as a status banner.
  await waitFor(() => {
    expect(screen.getByText("Citation decisions updated successfully.")).toBeInTheDocument();
  });
  expect(screen.queryByRole("button", { name: /Papers the AI suggests/i })).toBeNull();
  expect(screen.queryByTestId("candidate-card-solo2024")).toBeNull();
  expect(screen.getByTestId("candidate-sources-tray")).toBeInTheDocument();
});

test("test_error_banner_kept_visible_when_candidates_empty", async () => {
  const failingFetch = vi.fn(async () => {
    throw new Error("Candidates service unavailable");
  });
  render(
    <CandidateSourcesTray
      projectId="manuscript-2026"
      initialCandidates={[]}
      fetchImpl={failingFetch as unknown as typeof fetch}
    />
  );
  await waitFor(() => {
    expect(screen.getByRole("alert").textContent).toBe("Candidates service unavailable");
  });
  expect(screen.queryByRole("button", { name: /Papers the AI suggests/i })).toBeNull();
});

test("test_upload_input_uses_hidden_attribute_not_inline_style", () => {
  render(
    <CandidateSourcesTray projectId="default" initialCandidates={[makeCandidate("a2024")]} />
  );
  fireEvent.click(screen.getByRole("button", { name: "Papers the AI suggests (1)" }));
  const input = screen.getByTestId("candidate-sources-tray").querySelector("input") as any;
  expect(input === null).toBe(false);
  expect(Boolean(input.hasAttribute("hidden"))).toBe(true);
  expect(Boolean(input.getAttribute("style"))).toBe(false);
});

test("test_collapsed_by_default_shows_count", () => {
  render(
    <CandidateSourcesTray
      projectId="default"
      initialCandidates={[makeCandidate("a2024"), makeCandidate("b2024")]}
    />
  );
  const toggle = screen.getByRole("button", {
    name: "Papers the AI suggests (2)",
  });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByText(EXPLANATION)).toBeNull();
  expect(screen.queryByTestId("candidate-card-a2024")).toBeNull();
  expect(screen.queryByTestId("candidate-card-b2024")).toBeNull();
});

test("test_expand_shows_explanation_and_items", () => {
  render(
    <CandidateSourcesTray
      projectId="default"
      initialCandidates={[makeCandidate("a2024"), makeCandidate("b2024")]}
    />
  );
  fireEvent.click(
    screen.getByRole("button", { name: "Papers the AI suggests (2)" })
  );
  expect(
    screen
      .getByRole("button", { name: "Papers the AI suggests (2)" })
      .getAttribute("aria-expanded")
  ).toBe("true");
  expect(screen.getByText(EXPLANATION)).toBeInTheDocument();
  expect(screen.getByTestId("candidate-card-a2024")).toBeInTheDocument();
  expect(screen.getByTestId("candidate-card-b2024")).toBeInTheDocument();

  // Toggling again collapses
  fireEvent.click(
    screen.getByRole("button", { name: "Papers the AI suggests (2)" })
  );
  expect(screen.queryByText(EXPLANATION)).toBeNull();
});

test("test_plain_language_title", () => {
  render(
    <CandidateSourcesTray
      projectId="default"
      initialCandidates={[makeCandidate("a2024")]}
    />
  );
  expect(screen.getByText("Papers the AI suggests (1)")).toBeInTheDocument();
  expect(screen.queryByText(/Candidate Sources Staging Tray/i)).toBeNull();
  expect(screen.queryByText(/Staged papers never modify/i)).toBeNull();
});
