import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import { ZoteroWizardModal } from "../components/ZoteroWizardModal";

test("two-step zotero wizard completes flow from key verification to collection sync", async () => {
  const startTime = Date.now();
  let interactionCount = 0;

  const mockFetch = vi.fn(async (url: string) => {
    if (String(url).endsWith("/zotero")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          connected: false,
          collection_key: null,
          has_credentials: false,
          library_id: "",
          library_type: "user",
          push_enabled: false,
        }),
      };
    }
    if (String(url).includes("/api/zotero/verify-key")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          valid: true,
          has_read_scope: true,
          has_write_scope: false,
          collections: [
            { key: "COLL_PAPER", name: "Research Library", num_items: 18 },
            { key: "COLL_RAG", name: "RAG Benchmarks", num_items: 7 },
          ],
          injection_warnings: [],
          warning_message: "Connected with read-only Zotero key (recommended default).",
        }),
      };
    }
    if (String(url).includes("/zotero/sync")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          collection_key: "COLL_RAG",
          synced_bib_count: 7,
          synced_pdf_count: 4,
          key_remapping: {},
          injection_warnings: [],
          etag: "etag-zot-sync-1",
        }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });

  render(
    <ZoteroWizardModal
      isOpen={true}
      projectId="manuscript-2026"
      csrfToken="csrf-test-token"
      fetchImpl={mockFetch as unknown as typeof fetch}
    />
  );

  // Step 1 of 2: Enter credentials and verify key
  await screen.findByText(/Step 1 of 2/i);
  const pushCheckbox = screen.getByLabelText(/Push newly accepted citations to Zotero/i) as HTMLInputElement;
  expect(pushCheckbox.checked).toBe(false);

  fireEvent.change(screen.getByLabelText(/Zotero API Key/i), { target: { value: "zot-readonly-secret-key" } });
  fireEvent.change(screen.getByLabelText(/Zotero Library ID/i), { target: { value: "123456" } });
  fireEvent.click(screen.getByRole("button", { name: /Verify Key & Continue/i }));
  interactionCount += 1;

  // Step 2 of 2: Select Collection and Connect & Sync
  await waitFor(() => {
    expect(screen.getByText(/Step 2 of 2/i)).toBeInTheDocument();
  });
  fireEvent.change(screen.getByLabelText(/Zotero Collection/i), { target: { value: "COLL_RAG" } });
  fireEvent.click(screen.getByRole("button", { name: /Connect & Sync/i }));
  interactionCount += 1;

  await waitFor(() => {
    expect(screen.getByText(/Sync Complete/i)).toBeInTheDocument();
  });
  expect(screen.getByText(/7 BibTeX references/i)).toBeInTheDocument();
  expect(screen.getByText(/4 full-text PDFs/i)).toBeInTheDocument();

  const elapsedMs = Date.now() - startTime;
  expect(interactionCount).toBeLessThanOrEqual(2);
  expect(elapsedMs).toBeLessThan(60000);
});

test("shows connected status card when already linked and supports sync and disconnect", async () => {
  const onDisconnected = vi.fn();
  const mockFetch = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method || "GET";
    if (method === "GET" && String(url).endsWith("/zotero")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          connected: true,
          collection_key: "COLL_RAG",
          has_credentials: true,
          library_id: "123456",
          library_type: "user",
          push_enabled: false,
        }),
      };
    }
    if (method === "POST" && String(url).endsWith("/zotero/sync")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          collection_key: "COLL_RAG",
          synced_bib_count: 3,
          synced_pdf_count: 1,
          key_remapping: {},
          injection_warnings: [],
          etag: "etag-zot-sync-2",
        }),
      };
    }
    if (method === "POST" && String(url).endsWith("/zotero/disconnect")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ project_id: "manuscript-2026", zotero_collection_key: null }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });

  render(
    <ZoteroWizardModal
      isOpen={true}
      projectId="manuscript-2026"
      csrfToken="csrf-test-token"
      onDisconnected={onDisconnected}
      fetchImpl={mockFetch as unknown as typeof fetch}
    />
  );

  await screen.findByTestId("zotero-connected-view");
  expect(screen.getByText("COLL_RAG")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "Sync now" }));
  await screen.findByText(/3 BibTeX references/i);

  fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
  await waitFor(() => {
    expect(onDisconnected).toHaveBeenCalledTimes(1);
  });
});
