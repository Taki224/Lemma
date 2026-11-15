import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import { VersionHistoryDrawer } from "../components/VersionHistoryDrawer";

// Note: the test renderer re-creates DOM nodes on every state change, so
// elements are re-queried after interactions that update state.

const NEW_SHA = "sha-003-newest";
const MID_SHA = "sha-002-middle";
const OLD_SHA = "sha-001-oldest";

// Local-time constructors keep tests timezone independent.
function at(daysAgo: number, hour: number): string {
  const n = new Date();
  return new Date(n.getFullYear(), n.getMonth(), n.getDate() - daysAgo, hour, 0, 0).toISOString();
}

const snapshots = [
  { commit_sha: NEW_SHA, timestamp: at(0, 11), trigger: "compile_success", message: "Snapshot: build 7" },
  { commit_sha: MID_SHA, timestamp: at(0, 9), trigger: "ai_accept", message: "Accepted citations" },
  { commit_sha: OLD_SHA, timestamp: at(1, 15), trigger: "manual", message: "Initial commit" },
];

function preview(sha: string, diff: string, extra: Record<string, unknown> = {}) {
  return {
    commit_sha: sha,
    changed_files: ["main.tex"],
    unified_diff: diff,
    lost_bib_keys: [],
    has_bib_loss_warning: false,
    ...extra,
  };
}

const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body });

function makeFetch(overrides: {
  history?: () => Promise<unknown> | unknown;
  previews?: Record<string, () => Promise<unknown> | unknown>;
  restore?: () => Promise<unknown> | unknown;
} = {}) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    if (u.endsWith("/history")) {
      return overrides.history ? overrides.history() : ok({ snapshots });
    }
    if (u.endsWith("/restore") && init?.method === "POST") {
      return overrides.restore ? overrides.restore() : ok({ snapshot: {} });
    }
    const m = u.match(/\/history\/([^/]+)\/preview$/);
    if (m) {
      const custom = overrides.previews?.[m[1]];
      if (custom) return custom();
      return ok(preview(m[1], `diff-of-${m[1]}`));
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });
}

function setup(
  fetchImpl: ReturnType<typeof makeFetch>,
  props: Partial<React.ComponentProps<typeof VersionHistoryDrawer>> = {},
) {
  const onClose = vi.fn();
  const onRestoreCompleted = vi.fn();
  const view = render(
    <VersionHistoryDrawer
      isOpen
      projectId="proj-1"
      csrfToken="csrf-xyz"
      dirtyBuffers={{}}
      onClose={onClose}
      onRestoreCompleted={onRestoreCompleted}
      fetchImpl={fetchImpl as unknown as typeof fetch}
      {...props}
    />,
  );
  return { onClose, onRestoreCompleted, view };
}

const restoreButton = () => screen.getByRole("button", { name: /restore this version/i });
const confirmButton = () => screen.getByRole("button", { name: /^confirm/i });
const postCalls = (f: ReturnType<typeof makeFetch>) =>
  f.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST");

async function ready() {
  await waitFor(() => {
    expect(screen.getByText(/diff-of-/)).toBeInTheDocument();
  });
}

test("test_list_grouped_by_day_with_labels", async () => {
  setup(makeFetch());
  await waitFor(() => {
    expect(screen.getByText(/^Today$/)).toBeInTheDocument();
  });
  expect(screen.getByText(/^Yesterday$/)).toBeInTheDocument();
  expect(screen.getByText("Compiled")).toBeInTheDocument();
  expect(screen.getByText("AI edit accepted")).toBeInTheDocument();
  expect(screen.getByText("Saved manually")).toBeInTheDocument();
  // detail is shown, generic "Snapshot:" prefix stripped
  expect(screen.getByText("build 7")).toBeInTheDocument();
  expect(screen.getByText("Accepted citations")).toBeInTheDocument();
});

test("test_newest_selected_by_default", async () => {
  const f = makeFetch();
  setup(f);
  await ready();
  const rows = screen.getAllByRole("button").filter((b) => b.getAttribute("aria-current") === "true");
  expect(rows.length).toBe(1);
  expect(rows[0].textContent).toContain("Compiled");
  const previewUrls = f.mock.calls.map(([u]) => String(u)).filter((u) => u.endsWith("/preview"));
  expect(previewUrls).toEqual([`/api/projects/proj-1/history/${NEW_SHA}/preview`]);
});

test("test_select_loads_preview_diff", async () => {
  setup(makeFetch());
  await ready();
  expect(screen.getByText(`diff-of-${NEW_SHA}`)).toBeInTheDocument();
  fireEvent.click(screen.getByText("Saved manually"));
  await waitFor(() => {
    expect(screen.getByText(`diff-of-${OLD_SHA}`)).toBeInTheDocument();
  });
  expect(screen.queryByText(`diff-of-${NEW_SHA}`)).toBeNull();
});

test("test_stale_preview_response_ignored", async () => {
  let resolveNew: (v: unknown) => void = () => {};
  const f = makeFetch({
    previews: {
      [NEW_SHA]: () => new Promise((r) => { resolveNew = r; }),
    },
  });
  setup(f);
  await waitFor(() => {
    expect(screen.getByText("Saved manually")).toBeInTheDocument();
  });
  fireEvent.click(screen.getByText("Saved manually"));
  await waitFor(() => {
    expect(screen.getByText(`diff-of-${OLD_SHA}`)).toBeInTheDocument();
  });
  // The earlier (newest) response arrives late and must be ignored.
  resolveNew(ok(preview(NEW_SHA, "STALE-DIFF")));
  await new Promise((r) => setTimeout(r, 30));
  expect(screen.queryByText("STALE-DIFF")).toBeNull();
  expect(screen.getByText(`diff-of-${OLD_SHA}`)).toBeInTheDocument();
});

test("test_restore_requires_single_confirm", async () => {
  const f = makeFetch();
  const { onRestoreCompleted, onClose } = setup(f);
  await ready();
  fireEvent.click(restoreButton());
  expect(postCalls(f).length).toBe(0);
  expect(screen.getByText(/Restore this version\? Your current work is saved in a backup first\./)).toBeInTheDocument();
  fireEvent.click(confirmButton());
  await waitFor(() => {
    expect(onRestoreCompleted).toHaveBeenCalledWith(NEW_SHA);
  });
  expect(postCalls(f).length).toBe(1);
  expect(String(postCalls(f)[0][0])).toBe(`/api/projects/proj-1/history/${NEW_SHA}/restore`);
  expect(onClose).toHaveBeenCalled();
});

test("test_cancel_confirm_does_not_post", async () => {
  const f = makeFetch();
  const { onRestoreCompleted } = setup(f);
  await ready();
  fireEvent.click(restoreButton());
  fireEvent.click(screen.getByRole("button", { name: /^cancel/i }));
  expect(postCalls(f).length).toBe(0);
  expect(onRestoreCompleted).not.toHaveBeenCalled();
  expect(screen.queryByText(/saved in a backup first/)).toBeNull();
  expect(restoreButton()).toBeInTheDocument();
});

test("test_bib_loss_checkbox_gates_confirm", async () => {
  const f = makeFetch({
    previews: {
      [NEW_SHA]: () =>
        ok(preview(NEW_SHA, "bib-diff", { lost_bib_keys: ["vaswani2017", "brown2020"], has_bib_loss_warning: true })),
    },
  });
  setup(f);
  await waitFor(() => {
    expect(screen.getByText(/Warning: Restoring this version will remove bibliography entries/i)).toBeInTheDocument();
  });
  expect(screen.getByText("vaswani2017")).toBeInTheDocument();
  expect(screen.getByText("brown2020")).toBeInTheDocument();
  fireEvent.click(restoreButton());
  expect(confirmButton().getAttribute("disabled")).toBe("true");
  fireEvent.click(confirmButton());
  expect(postCalls(f).length).toBe(0);

  fireEvent.change(screen.getByLabelText(/I confirm that references\.bib entries will be removed/i), {
    target: { checked: true },
  });
  expect(confirmButton().getAttribute("disabled")).toBe("false");
  fireEvent.click(confirmButton());
  await waitFor(() => {
    expect(postCalls(f).length).toBe(1);
  });
});

test("test_restore_sends_all_dirty_buffers", async () => {
  const f = makeFetch();
  const dirty = { "main.tex": "edited main", "chapters/a.tex": "edited a" };
  setup(f, { dirtyBuffers: dirty });
  await ready();
  fireEvent.click(restoreButton());
  expect(screen.getByText(/Your unsaved edits \(2 files\) are saved in a backup first\./)).toBeInTheDocument();
  fireEvent.click(confirmButton());
  await waitFor(() => {
    expect(postCalls(f).length).toBe(1);
  });
  const init = postCalls(f)[0][1] as RequestInit;
  expect(JSON.parse(String(init.body))).toEqual({ unsaved_buffers: dirty });
  expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-xyz");
});

test("test_error_shown_on_failure", async () => {
  const f = makeFetch({
    restore: () => ({ ok: false, status: 500, json: async () => ({ detail: "Disk full" }) }),
  });
  const { onRestoreCompleted, onClose } = setup(f);
  await ready();
  fireEvent.click(restoreButton());
  fireEvent.click(confirmButton());
  await waitFor(() => {
    expect(screen.getByText("Disk full")).toBeInTheDocument();
  });
  expect(screen.getByText("Disk full").getAttribute("class")).toContain("lemma-error-banner");
  expect(onRestoreCompleted).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
});

test("test_escape_closes_when_not_restoring", async () => {
  let resolveRestore: (v: unknown) => void = () => {};
  const f = makeFetch({ restore: () => new Promise((r) => { resolveRestore = r; }) });
  const { onClose } = setup(f);
  await ready();

  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);

  fireEvent.click(restoreButton());
  fireEvent.click(confirmButton());
  await waitFor(() => {
    expect(postCalls(f).length).toBe(1);
  });
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  expect(onClose).toHaveBeenCalledTimes(1);
  resolveRestore(ok({}));
});

test("test_empty_and_loading_states", async () => {
  let resolveHistory: (v: unknown) => void = () => {};
  const loading = makeFetch({ history: () => new Promise((r) => { resolveHistory = r; }) });
  setup(loading);
  expect(screen.getByText(/Loading/i).getAttribute("class")).toContain("lemma-empty-state");
  resolveHistory(ok({ snapshots: [] }));
  await waitFor(() => {
    expect(screen.getByText(/No version snapshots/i)).toBeInTheDocument();
  });
  expect(screen.getByText(/No version snapshots/i).getAttribute("class")).toContain("lemma-empty-state");
});

test("test_zero_files_clause_hidden_without_unsaved_edits", async () => {
  setup(makeFetch());
  await ready();
  fireEvent.click(restoreButton());
  expect(screen.queryByText(/\(0 files\)/)).toBeNull();
  expect(screen.queryByText(/Your unsaved edits/)).toBeNull();
});

function drawerJsx(fetchImpl: ReturnType<typeof makeFetch>, isOpen: boolean) {
  return (
    <VersionHistoryDrawer
      isOpen={isOpen}
      projectId="proj-1"
      csrfToken="csrf-xyz"
      dirtyBuffers={{}}
      onClose={() => {}}
      fetchImpl={fetchImpl as unknown as typeof fetch}
    />
  );
}

test("test_close_and_reopen_resets_confirm_preview_and_snapshots", async () => {
  let historyCalls = 0;
  const f = makeFetch({
    history: () => {
      historyCalls++;
      return historyCalls === 1 ? ok({ snapshots }) : new Promise(() => {});
    },
  });
  const { view } = setup(f);
  await ready();
  fireEvent.click(screen.getByText("Saved manually"));
  await waitFor(() => {
    expect(screen.getByText(`diff-of-${OLD_SHA}`)).toBeInTheDocument();
  });
  fireEvent.click(restoreButton());
  expect(screen.getByText(/saved in a backup first/)).toBeInTheDocument();

  view.rerender(drawerJsx(f, false));
  expect(screen.queryByRole("dialog")).toBeNull();
  view.rerender(drawerJsx(f, true));

  // Second open: history is still loading, nothing from the previous session may leak through.
  expect(screen.getByText("Loading version history...")).toBeInTheDocument();
  expect(screen.queryByText("Saved manually")).toBeNull();
  expect(screen.queryByText(/saved in a backup first/)).toBeNull();
  expect(screen.queryByText(/diff-of-/)).toBeNull();
});

test("test_reclicking_selected_row_retries_failed_preview", async () => {
  let attempts = 0;
  const f = makeFetch({
    previews: {
      [NEW_SHA]: () => {
        attempts++;
        return attempts === 1 ? { ok: false, status: 500, json: async () => ({}) } : ok(preview(NEW_SHA, "retried-diff"));
      },
    },
  });
  setup(f);
  await waitFor(() => {
    expect(screen.getByText(/Failed to load restore preview/)).toBeInTheDocument();
  });
  fireEvent.click(screen.getByText("Compiled"));
  await waitFor(() => {
    expect(screen.getByText("retried-diff")).toBeInTheDocument();
  });
  expect(attempts).toBe(2);
  expect(screen.queryByText(/Failed to load restore preview/)).toBeNull();
});

test("test_sha_is_url_encoded_in_preview_and_restore_urls", async () => {
  const weird = "sha with/slash";
  const f = makeFetch({
    history: () => ok({ snapshots: [{ commit_sha: weird, timestamp: at(0, 11), trigger: "manual", message: "x" }] }),
  });
  setup(f);
  await ready();
  fireEvent.click(restoreButton());
  fireEvent.click(confirmButton());
  await waitFor(() => {
    expect(postCalls(f).length).toBe(1);
  });
  const encoded = encodeURIComponent(weird);
  expect(f.mock.calls.map(([u]) => String(u))).toContain(`/api/projects/proj-1/history/${encoded}/preview`);
  expect(String(postCalls(f)[0][0])).toBe(`/api/projects/proj-1/history/${encoded}/restore`);
});
