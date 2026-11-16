import React from "react";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import { App } from "../App";

test("test_renders_login_screen_when_unauthenticated_and_submits_password", async () => {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/auth/status") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          authenticated: false,
          csrf_token: null,
          insecure_http_warning: false,
          privacy_consent_accepted: false,
        }),
      };
    }
    if (url === "/api/auth/login" && init?.method === "POST") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          authenticated: true,
          csrf_token: "csrf-login-token-123",
          insecure_http_warning: false,
          privacy_consent_accepted: false,
        }),
      };
    }
    if (url === "/api/settings/consent" && init?.method === "POST") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ privacy_consent_accepted: true }),
      };
    }
    if (url === "/api/projects") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ projects: [] }),
      };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });

  vi.stubGlobal("fetch", fetchMock);

  render(<App />);

  const passwordInput = await screen.findByRole("textbox", { name: "Workspace Password" });
  expect(passwordInput).toBeInTheDocument();

  fireEvent.change(passwordInput, { target: { value: "my-secret-password" } });

  const signInBtn = screen.getByRole("button", { name: "Sign In" });
  fireEvent.click(signInBtn);

  await waitFor(() => {
    expect(localStorage.getItem("lemma_csrf_token")).toBe("csrf-login-token-123");
  });

  // First-run privacy consent modal appears and submits POST /api/settings/consent with X-CSRF-Token
  const acceptConsentBtn = await screen.findByRole("button", { name: "Accept Privacy Notice" });
  fireEvent.click(acceptConsentBtn);

  await waitFor(() => {
    const consentCall = fetchMock.mock.calls.find((c) => c[0] === "/api/settings/consent");
    expect(consentCall).toBeDefined();
    expect(consentCall?.[1]?.headers).toEqual({
      "Content-Type": "application/json",
      "X-CSRF-Token": "csrf-login-token-123",
    });
  });

  // Verify three-pane workspace and mode toggle render (modes live in Tools)
  fireEvent.click(screen.getByRole("button", { name: "Tools" }));
  expect(screen.getByRole("button", { name: "Editor" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Review Studio" })).toBeInTheDocument();
});

test("test_displays_http_warning_banner_when_flag_set", async () => {
  const fetchMock = vi.fn(async (url: string) => {
    if (url === "/api/auth/status") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          authenticated: true,
          csrf_token: "csrf-abc",
          insecure_http_warning: true,
          privacy_consent_accepted: true,
        }),
      };
    }
    return { ok: true, status: 200, json: async () => ({ projects: [] }) };
  });

  vi.stubGlobal("fetch", fetchMock);

  render(<App />);

  const banner = await screen.findByText(
    "Warning: Workspace is served over unencrypted HTTP on a non-localhost address. Configure an HTTPS reverse proxy."
  );
  expect(banner).toBeInTheDocument();
});

function makeExistingBuildFetchMock(pdfStatus: 200 | 404) {
  return vi.fn(async (url: string) => {
    if (url === "/api/auth/status") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          authenticated: true,
          csrf_token: "csrf-abc",
          insecure_http_warning: false,
          privacy_consent_accepted: true,
        }),
      };
    }
    if (url === "/api/projects") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          projects: [{ project_id: "manuscript-2026", name: "Manuscript", root_tex: "main.tex" }],
        }),
      };
    }
    if (url === "/api/projects/manuscript-2026/pdf") {
      return pdfStatus === 200
        ? { ok: true, status: 200, json: async () => ({}) }
        : { ok: false, status: 404, json: async () => ({ detail: "Compiled PDF not found" }) };
    }
    if (url === "/api/projects/manuscript-2026/files") {
      return { ok: true, status: 200, json: async () => ({ files: [] }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
}

test("test_app_shows_existing_compiled_pdf_on_load_without_recompile", async () => {
  const fetchMock = makeExistingBuildFetchMock(200);
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);

  // Workspace loaded and the project became active (file list requested).
  await waitFor(() => {
    expect(fetchMock.mock.calls.some((c) => c[0] === "/api/projects/manuscript-2026/files")).toBe(true);
  });

  // No compile click: the existing build must be discovered via GET /api/projects/{id}/pdf.
  await waitFor(() => {
    const downloadLink = screen.getByRole("link", { name: "Download PDF" });
    expect(downloadLink).toHaveAttribute("href", "/api/projects/manuscript-2026/pdf");
  });
  expect(screen.queryByText(/No PDF has been compiled/i)).toBeNull();
});

test("test_app_probes_pdf_endpoint_on_load_and_keeps_idle_without_error_on_404", async () => {
  const fetchMock = makeExistingBuildFetchMock(404);
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);

  await waitFor(() => {
    expect(fetchMock.mock.calls.some((c) => c[0] === "/api/projects/manuscript-2026/files")).toBe(true);
  });

  // The frontend must probe for an existing build when the active project is set.
  await waitFor(() => {
    expect(fetchMock.mock.calls.some((c) => c[0] === "/api/projects/manuscript-2026/pdf")).toBe(true);
  });

  // 404 => still the empty state, no error badge, no download link.
  expect(screen.getByText(/No PDF has been compiled/i)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Download PDF" })).toBeNull();
  expect(screen.getByRole("button", { name: "Compile Status: Not Compiled" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Compile Status: .*Error/i })).toBeNull();
});

test("test_settings_button_opens_models_view_and_enabled_model_appears_in_chat_bar_dropdown", async () => {
  let enabled: any[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/auth/status") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          authenticated: true,
          csrf_token: "csrf-abc",
          insecure_http_warning: false,
          privacy_consent_accepted: true,
        }),
      };
    }
    if (url === "/api/projects") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          projects: [{ project_id: "manuscript-2026", name: "Manuscript", root_tex: "main.tex" }],
        }),
      };
    }
    if (url === "/api/ai/models") {
      return { ok: true, status: 200, json: async () => ({ models: enabled }) };
    }
    if (url.startsWith("/api/settings/models/catalog")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          models: [
            {
              id: "x/new",
              name: "Brand New Model",
              context_length: 100000,
              prompt_price_per_token: 0.000003,
              completion_price_per_token: 0.000015,
              supports_vision: false,
            },
          ],
          catalog_available: true,
        }),
      };
    }
    if (url === "/api/settings/models" && method === "POST") {
      enabled = [
        {
          id: "x/new",
          name: "Brand New Model",
          supports_vision: false,
          verified: true,
          prompt_price_per_token: 0.000003,
          completion_price_per_token: 0.000015,
        },
      ];
      return { ok: true, status: 201, json: async () => ({}) };
    }
    if (url === "/api/projects/manuscript-2026/files") {
      return { ok: true, status: 200, json: async () => ({ files: [] }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);

  const toolsBtn = await screen.findByRole("button", { name: "Tools" });
  fireEvent.click(toolsBtn);
  const settingsBtn = screen.getByRole("button", { name: "Settings" });
  fireEvent.click(settingsBtn);

  await waitFor(() => {
    expect(Boolean(screen.getByTestId("models-settings-view"))).toBe(true);
  });

  const enableBtn = await screen.findByRole("button", { name: "Enable x/new" });
  fireEvent.click(enableBtn);

  await waitFor(() => {
    const post = fetchMock.mock.calls.find((c) => c[0] === "/api/settings/models" && c[1]?.method === "POST");
    expect(post?.[1]?.headers).toEqual({
      "Content-Type": "application/json",
      "X-CSRF-Token": "csrf-abc",
    });
  });

  fireEvent.click(screen.getByRole("button", { name: "Editor" }));

  await waitFor(() => {
    expect(screen.getByLabelText("OpenRouter Model").querySelectorAll("option").length).toBe(1);
  });
  const modelSelect = screen.getByLabelText("OpenRouter Model");
  expect(modelSelect.querySelector("option")?.textContent).toBe("Brand New Model");
});

test("test_create_project_via_plus_selects_new_project", async () => {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/auth/status") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          authenticated: true,
          csrf_token: "csrf-abc",
          insecure_http_warning: false,
          privacy_consent_accepted: true,
        }),
      };
    }
    if (url === "/api/projects" && method === "POST") {
      return {
        ok: true,
        status: 201,
        json: async () => ({ project_id: "new-paper", name: "New Paper", root_tex: "main.tex" }),
      };
    }
    if (url === "/api/projects") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          projects: [{ project_id: "manuscript-2026", name: "Manuscript", root_tex: "main.tex" }],
        }),
      };
    }
    if (url.endsWith("/files")) {
      return { ok: true, status: 200, json: async () => ({ files: [] }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);

  fireEvent.click(await screen.findByRole("button", { name: "New project" }));
  fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "New Paper" } });
  fireEvent.click(screen.getByRole("button", { name: /create/i }));

  await waitFor(() => {
    const post = fetchMock.mock.calls.find((c) => c[0] === "/api/projects" && c[1]?.method === "POST");
    expect(JSON.parse(post?.[1]?.body as string)).toEqual({ name: "New Paper" });
  });
  await waitFor(() => {
    expect(screen.queryByRole("dialog")).toBeNull();
    expect((screen.getByLabelText("Select Project") as HTMLSelectElement).value).toBe("new-paper");
  });
});

test("test_create_project_error_cleared_on_cancel_and_reopen", async () => {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/auth/status") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          authenticated: true,
          csrf_token: "csrf-abc",
          insecure_http_warning: false,
          privacy_consent_accepted: true,
        }),
      };
    }
    if (url === "/api/projects" && method === "POST") {
      return { ok: false, status: 409, json: async () => ({ detail: "Project already exists" }) };
    }
    if (url === "/api/projects") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ projects: [{ project_id: "manuscript-2026", name: "Manuscript", root_tex: "main.tex" }] }),
      };
    }
    if (url.endsWith("/files")) {
      return { ok: true, status: 200, json: async () => ({ files: [] }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);

  fireEvent.click(await screen.findByRole("button", { name: "New project" }));
  fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "Dup" } });
  fireEvent.click(screen.getByRole("button", { name: /create/i }));
  await waitFor(() => {
    expect(screen.getByText("Project already exists")).toBeInTheDocument();
  });

  fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
  fireEvent.click(screen.getByRole("button", { name: "New project" }));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(screen.queryByText("Project already exists")).toBeNull();
});

// ---- Task 1.6: stale tracking and forward-sync feedback ----

function makeForwardSyncFetchMock(forward: () => any) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/auth/status") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          authenticated: true,
          csrf_token: "csrf-abc",
          insecure_http_warning: false,
          privacy_consent_accepted: true,
        }),
      };
    }
    if (url === "/api/projects") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          projects: [{ project_id: "manuscript-2026", name: "Manuscript", root_tex: "main.tex" }],
        }),
      };
    }
    if (url === "/api/projects/manuscript-2026/pdf") {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    if (url === "/api/projects/manuscript-2026/files") {
      return { ok: true, status: 200, json: async () => ({ files: [] }) };
    }
    if (url === "/api/projects/manuscript-2026/files/main.tex") {
      return { ok: true, status: 200, json: async () => ({ content: "hello", etag: "e1" }) };
    }
    if (url === "/api/projects/manuscript-2026/compile" && method === "POST") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          status: "success",
          pdf_url: "/api/projects/manuscript-2026/pdf",
          diagnostics: [],
        }),
      };
    }
    if (url.startsWith("/api/projects/manuscript-2026/synctex/forward")) {
      return forward();
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
}

const exactForwardBox = {
  page: 1,
  x: 72,
  y: 240,
  h: 72,
  v: 252,
  width: 100,
  height: 20,
  exact: true,
  hint: null,
};

async function renderAppWithPdf(fetchMock: any) {
  vi.stubGlobal("fetch", fetchMock);
  render(<App />);
  await waitFor(() => {
    expect(screen.getByRole("link", { name: "Download PDF" })).toBeInTheDocument();
  });
  await waitFor(() => {
    expect(fetchMock.mock.calls.some((c: any) => c[0] === "/api/projects/manuscript-2026/files/main.tex")).toBe(true);
  });
}

async function clickShowInPdf() {
  fireEvent.click(screen.getByRole("button", { name: /Show in PDF/i }));
}

// The fake CodeMirror never invokes update listeners: capture the editor's listener to simulate an edit.
async function simulateEditorEdit(capture: { fns: Array<(u: any) => void> }, text: string) {
  const fn = capture.fns[capture.fns.length - 1];
  fn({
    docChanged: true,
    selectionSet: false,
    state: {
      doc: { toString: () => text, lineAt: () => ({ number: 1 }) },
      selection: { main: { head: 0, from: 0, to: 0 } },
      sliceDoc: () => "",
    },
  });
}

test("test_forward_sync_404_shows_compile_first", async () => {
  const fetchMock = makeForwardSyncFetchMock(() => ({
    ok: false,
    status: 404,
    json: async () => ({ detail: "not compiled" }),
  }));
  await renderAppWithPdf(fetchMock);

  await clickShowInPdf();

  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("Compile first");
  });
});

test("test_forward_sync_error_shows_generic_notice", async () => {
  const fetchMock = makeForwardSyncFetchMock(() => ({
    ok: false,
    status: 500,
    json: async () => ({}),
  }));
  await renderAppWithPdf(fetchMock);
  await clickShowInPdf();
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("Could not locate this line in the PDF");
  });
});

test("test_forward_sync_network_error_shows_generic_notice", async () => {
  const fetchMock = makeForwardSyncFetchMock(() => {
    throw new Error("network down");
  });
  await renderAppWithPdf(fetchMock);
  await clickShowInPdf();
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("Could not locate this line in the PDF");
  });
});

test("test_inexact_match_shows_out_of_date_hint", async () => {
  const fetchMock = makeForwardSyncFetchMock(() => ({
    ok: true,
    status: 200,
    json: async () => ({ ...exactForwardBox, exact: false, hint: "nearest position" }),
  }));
  await renderAppWithPdf(fetchMock);
  await clickShowInPdf();
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("nearest position — PDF may be out of date");
  });
});

test("test_inexact_match_without_hint_shows_only_out_of_date", async () => {
  const fetchMock = makeForwardSyncFetchMock(() => ({
    ok: true,
    status: 200,
    json: async () => ({ ...exactForwardBox, exact: false, hint: null }),
  }));
  await renderAppWithPdf(fetchMock);
  await clickShowInPdf();
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("PDF may be out of date");
  });
});

// Captures the editor's update listener (the fake CodeMirror never invokes it) and restores it afterwards.
async function withEditorEditCapture(run: (capture: { fns: Array<(u: any) => void> }) => Promise<void>) {
  const capture = { fns: [] as Array<(u: any) => void> };
  const { EditorView } = await import("@codemirror/view");
  const originalOf = (EditorView as any).updateListener.of;
  (EditorView as any).updateListener.of = (fn: any) => {
    capture.fns.push(fn);
    return originalOf(fn);
  };
  try {
    await run(capture);
  } finally {
    (EditorView as any).updateListener.of = originalOf;
  }
}

const stalePane = () => screen.getByTestId("right-pdf-pane").children?.[0] as any;

test("test_exact_match_without_edit_shows_no_notice_and_not_stale", async () => {
  await withEditorEditCapture(async () => {
    const fetchMock = makeForwardSyncFetchMock(() => ({ ok: true, status: 200, json: async () => exactForwardBox }));
    await renderAppWithPdf(fetchMock);
    expect(stalePane().getAttribute("data-stale")).toBe(null);
    await clickShowInPdf();
    await waitFor(() => {
      expect(screen.queryByTestId("synctex-highlight-box") !== null).toBe(true);
    });
    expect(screen.queryByRole("status")).toBeNull();
  });
});

test("test_edit_after_compile_marks_stale_and_warns_on_exact_match", async () => {
  await withEditorEditCapture(async (capture) => {
    const fetchMock = makeForwardSyncFetchMock(() => ({ ok: true, status: 200, json: async () => exactForwardBox }));
    await renderAppWithPdf(fetchMock);
    await simulateEditorEdit(capture, "hello edited");
    await waitFor(() => {
      expect(stalePane().getAttribute("data-stale")).toBe("true");
    });
    await clickShowInPdf();
    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toBe("PDF may be out of date");
    });
  });
});

test("test_successful_compile_clears_stale", async () => {
  await withEditorEditCapture(async (capture) => {
    const fetchMock = makeForwardSyncFetchMock(() => ({ ok: true, status: 200, json: async () => exactForwardBox }));
    await renderAppWithPdf(fetchMock);
    await simulateEditorEdit(capture, "hello edited");
    await waitFor(() => {
      expect(stalePane().getAttribute("data-stale")).toBe("true");
    });
    fireEvent.click(screen.getByRole("button", { name: "Recompile" }));
    await waitFor(() => {
      expect(stalePane().getAttribute("data-stale")).toBe(null);
    });
    await clickShowInPdf();
    await waitFor(() => {
      expect(screen.queryByTestId("synctex-highlight-box") !== null).toBe(true);
    });
    expect(screen.queryByRole("status")).toBeNull();
  });
});

test("test_recompile_saves_dirty_tex_file_before_compiling", async () => {
  await withEditorEditCapture(async (capture) => {
    const callOrder: string[] = [];
    const baseMock = makeForwardSyncFetchMock(() => ({ ok: true, status: 200, json: async () => exactForwardBox }));
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/projects/manuscript-2026/files/main.tex" && method === "PUT") {
        callOrder.push(`PUT:${JSON.parse(String(init?.body)).content}`);
        return { ok: true, status: 200, json: async () => ({ etag: "e2" }) };
      }
      if (url === "/api/projects/manuscript-2026/compile" && method === "POST") {
        callOrder.push("COMPILE");
      }
      return baseMock(url, init);
    });
    await renderAppWithPdf(fetchMock);
    await simulateEditorEdit(capture, "hello edited");
    await waitFor(() => {
      expect(stalePane().getAttribute("data-stale")).toBe("true");
    });
    fireEvent.click(screen.getByRole("button", { name: "Recompile" }));
    await waitFor(() => {
      expect(stalePane().getAttribute("data-stale")).toBe(null);
    });
    expect(callOrder).toEqual(["PUT:hello edited", "COMPILE"]);
  });
});

test("test_recompile_aborts_compile_when_save_fails", async () => {
  await withEditorEditCapture(async (capture) => {
    const callOrder: string[] = [];
    const baseMock = makeForwardSyncFetchMock(() => ({ ok: true, status: 200, json: async () => exactForwardBox }));
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/projects/manuscript-2026/files/main.tex" && method === "PUT") {
        callOrder.push("PUT");
        return { ok: false, status: 500, json: async () => ({ detail: "Save failed" }) };
      }
      if (url === "/api/projects/manuscript-2026/compile" && method === "POST") {
        callOrder.push("COMPILE");
      }
      return baseMock(url, init);
    });
    await renderAppWithPdf(fetchMock);
    await simulateEditorEdit(capture, "hello edited");
    await waitFor(() => {
      expect(stalePane().getAttribute("data-stale")).toBe("true");
    });
    fireEvent.click(screen.getByRole("button", { name: "Recompile" }));
    await waitFor(() => {
      expect(callOrder).toEqual(["PUT"]);
      expect(screen.getByRole("button", { name: "Recompile" })).not.toBeDisabled();
    });
    expect(stalePane().getAttribute("data-stale")).toBe("true");
  });
});

test("test_success_passes_width_height_nonce_to_pdf_preview", async () => {
  const fetchMock = makeForwardSyncFetchMock(() => ({
    ok: true,
    status: 200,
    json: async () => exactForwardBox,
  }));
  await renderAppWithPdf(fetchMock);

  const proto = Object.getPrototypeOf(screen.getByTestId("pdf-page-surface-1"));
  const originalScroll = proto.scrollIntoView;
  let scrolls = 0;
  proto.scrollIntoView = function () {
    scrolls++;
  };
  const originalNow = Date.now;
  try {
    // Frozen clock: a Date.now()-based nonce would not change between syncs.
    const frozen = originalNow.call(Date);
    Date.now = () => frozen;

    await clickShowInPdf();
    await waitFor(() => {
      expect(screen.queryByTestId("synctex-highlight-box") !== null).toBe(true);
    });
    const box = screen.getByTestId("synctex-highlight-box") as any;
    expect(box.style.width).toBe("100px");
    expect(box.style.height).toBe("20px");
    expect(scrolls).toBe(1);

    await clickShowInPdf();
    await waitFor(() => {
      expect(scrolls).toBe(2);
    });
  } finally {
    Date.now = originalNow;
    proto.scrollIntoView = originalScroll;
  }
});

// ---- Pane layout (FEAT-003 Task 2.4) ----
// Pane splitters only: the chat panel's horizontal height handle is also a separator.
function paneHandles() {
  return screen
    .getAllByRole("separator")
    .filter((el: any) => el.getAttribute("aria-orientation") !== "horizontal");
}

// The fake renderer has no pointer events: invoke the separator's React handlers directly.
function dragSeparator(index: number, fromX: number, toX: number) {
  const el = paneHandles()[index];
  const makeEvent = (type: string, clientX: number) => ({
    type,
    pointerId: 1,
    clientX,
    clientY: 0,
    target: el,
    currentTarget: { setPointerCapture() {}, releasePointerCapture() {} },
    preventDefault() {},
    stopPropagation() {},
  });
  el.props.onPointerDown(makeEvent("pointerdown", fromX));
  // Re-query: App re-renders (new nodes) after each state change.
  paneHandles()[index].props.onPointerMove(makeEvent("pointermove", toX));
  paneHandles()[index].props.onPointerUp(makeEvent("pointerup", toX));
}

async function renderWorkspace() {
  vi.stubGlobal("fetch", makeExistingBuildFetchMock(404));
  render(<App />);
  await waitFor(() => {
    expect(paneHandles().length).toBe(2);
  });
}

test("test_panes_render_with_two_handles", async () => {
  await renderWorkspace();
  const handles = paneHandles();
  expect(handles.length).toBe(2);
  const grid = screen.getByTestId("pane-grid") as any;
  expect(grid.props.style["--pane-left-w"]).toBe("260px");
  expect(grid.props.style["--pane-right-w"]).toBe("420px");
  expect(Boolean(screen.getByTestId("left-sidebar-pane").props.hidden)).toBe(false);
  expect(Boolean(screen.getByTestId("right-pdf-pane").props.hidden)).toBe(false);
});

test("test_dragging_handle_updates_pane_width_css_var", async () => {
  await renderWorkspace();
  dragSeparator(0, 100, 140); // left handle: +40px => left pane grows
  expect((screen.getByTestId("pane-grid") as any).props.style["--pane-left-w"]).toBe("300px");
  dragSeparator(1, 500, 540); // right handle: +40px => right pane shrinks
  expect((screen.getByTestId("pane-grid") as any).props.style["--pane-right-w"]).toBe("380px");
});

test("test_dblclick_handle_collapses_and_restores_pane", async () => {
  await renderWorkspace();
  fireEvent.doubleClick(paneHandles()[0]);
  expect(screen.getByTestId("left-sidebar-pane").props.hidden).toBe(true);
  // Handle stays available so the pane can be restored.
  expect(paneHandles().length).toBe(2);
  fireEvent.doubleClick(paneHandles()[0]);
  expect(Boolean(screen.getByTestId("left-sidebar-pane").props.hidden)).toBe(false);

  fireEvent.doubleClick(paneHandles()[1]);
  expect(screen.getByTestId("right-pdf-pane").props.hidden).toBe(true);
  fireEvent.doubleClick(paneHandles()[1]);
  expect(Boolean(screen.getByTestId("right-pdf-pane").props.hidden)).toBe(false);
});

test("test_layout_restored_after_remount", async () => {
  await renderWorkspace();
  dragSeparator(0, 100, 140);
  fireEvent.doubleClick(paneHandles()[1]);
  cleanup();

  await renderWorkspace();
  expect((screen.getByTestId("pane-grid") as any).props.style["--pane-left-w"]).toBe("300px");
  expect(screen.getByTestId("right-pdf-pane").props.hidden).toBe(true);
});

test("test_editor_state_preserved_when_pane_hidden", async () => {
  await renderWorkspace();
  fireEvent.doubleClick(paneHandles()[0]);
  fireEvent.doubleClick(paneHandles()[1]);
  // Hidden via the `hidden` attribute, not unmounted: contents (and their state) stay mounted.
  const left = screen.getByTestId("left-sidebar-pane") as any;
  const right = screen.getByTestId("right-pdf-pane") as any;
  expect(left.props.hidden).toBe(true);
  expect(right.props.hidden).toBe(true);
  expect(left.querySelector(".lemma-left-sidebar") !== null).toBe(true);
  expect(right.querySelector(".lemma-pdf-preview-pane") !== null).toBe(true);
  expect(Boolean(screen.getByTestId("center-editor-pane").props.hidden)).toBe(false);
});

// ---- Tools menu pane toggles (FEAT-003 Task 2.5) ----
const toggleBtn = (name: string) => screen.getByRole("button", { name }) as any;
const openWorkspaceTools = () => {
  const tools = screen.getByRole("button", { name: "Tools" });
  if (tools.getAttribute("aria-expanded") !== "true") fireEvent.click(tools);
};

test("test_topbar_toggle_hides_pane", async () => {
  await renderWorkspace();
  openWorkspaceTools();
  expect(toggleBtn("Toggle sidebar").props["aria-pressed"]).toBe(true);
  fireEvent.click(toggleBtn("Toggle sidebar"));
  expect(screen.getByTestId("left-sidebar-pane").props.hidden).toBe(true);
  expect(toggleBtn("Toggle sidebar").props["aria-pressed"]).toBe(false);
  fireEvent.click(toggleBtn("Toggle sidebar"));
  expect(Boolean(screen.getByTestId("left-sidebar-pane").props.hidden)).toBe(false);

  fireEvent.click(toggleBtn("Toggle editor"));
  expect(screen.getByTestId("center-editor-pane").props.hidden).toBe(true);
  fireEvent.click(toggleBtn("Toggle PDF preview"));
  expect(screen.getByTestId("right-pdf-pane").props.hidden).toBe(true);
});

test("test_last_visible_pane_toggle_disabled", async () => {
  await renderWorkspace();
  openWorkspaceTools();
  fireEvent.click(toggleBtn("Toggle sidebar"));
  fireEvent.click(toggleBtn("Toggle PDF preview"));
  const last = toggleBtn("Toggle editor");
  expect(last.props.disabled).toBe(true);
  expect(last.props.title).toBe("At least one pane must stay visible");
  expect(Boolean(toggleBtn("Toggle sidebar").props.disabled)).toBe(false);
});

test("test_toggles_hidden_outside_editor_mode", async () => {
  await renderWorkspace();
  openWorkspaceTools();
  fireEvent.click(screen.getByRole("button", { name: "Review Studio" }));
  expect(screen.queryByRole("button", { name: "Toggle sidebar" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Toggle editor" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Toggle PDF preview" })).toBeNull();
});

// ---- Layout + bubble end-to-end (FEAT-003 Task 2.9) ----
test("test_layout_and_bubble_persist_together", async () => {
  await renderWorkspace();
  dragSeparator(0, 100, 140);
  fireEvent.doubleClick(paneHandles()[1]);
  fireEvent.click(screen.getByRole("button", { name: "Minimise to bubble" }));
  expect(Boolean(screen.getByRole("button", { name: "Open AI assistant" }))).toBe(true);
  cleanup();

  await renderWorkspace();
  expect((screen.getByTestId("pane-grid") as any).props.style["--pane-left-w"]).toBe("300px");
  expect(screen.getByTestId("right-pdf-pane").props.hidden).toBe(true);
  expect(Boolean(screen.getByRole("button", { name: "Open AI assistant" }))).toBe(true);
  expect(screen.getByTestId("floating-chat-bar").props.hidden).toBe(true);
});

test("test_narrow_window_keeps_pdf_visible_without_overwriting_preference", async () => {
  const seeded = JSON.stringify({
    widths: { left: 260, right: 420 },
    visible: { left: true, center: true, right: true },
  });
  localStorage.setItem("lemma_pane_layout_v1", seeded);
  const originalWidth = window.innerWidth;
  (window as any).innerWidth = 600;
  try {
    await renderWorkspace();
    expect(Boolean(screen.getByTestId("right-pdf-pane").props.hidden)).toBe(false);
    expect(Boolean(screen.getByTestId("left-sidebar-pane").props.hidden)).toBe(false);
    expect(Boolean(screen.getByTestId("center-editor-pane").props.hidden)).toBe(false);
    expect((screen.getByTestId("pane-grid") as any).props.style["--pane-right-w"]).toBe("259px");
    expect(localStorage.getItem("lemma_pane_layout_v1")).toBe(seeded);
    cleanup();
  } finally {
    (window as any).innerWidth = originalWidth;
  }

  await renderWorkspace();
  expect(Boolean(screen.getByTestId("right-pdf-pane").props.hidden)).toBe(false);
  expect((screen.getByTestId("pane-grid") as any).props.style["--pane-right-w"]).toBe("420px");
});

// ---- Review fixes: effective visibility, hidden center ----
test("test_narrow_window_pdf_toggle_stays_enabled", async () => {
  const seeded = JSON.stringify({
    widths: { left: 260, right: 420 },
    visible: { left: true, center: true, right: true },
  });
  localStorage.setItem("lemma_pane_layout_v1", seeded);
  const originalWidth = window.innerWidth;
  (window as any).innerWidth = 600;
  try {
    await renderWorkspace();
    openWorkspaceTools();
    const pdf = toggleBtn("Toggle PDF preview");
    expect(pdf.props["aria-pressed"]).toBe(true);
    expect(pdf.props.disabled).toBe(false);
    fireEvent.click(pdf);
    expect(screen.getByTestId("right-pdf-pane").props.hidden).toBe(true);
    fireEvent.click(pdf);
    expect(Boolean(screen.getByTestId("right-pdf-pane").props.hidden)).toBe(false);
  } finally {
    (window as any).innerWidth = originalWidth;
  }
});

test("test_assistant_stays_outside_center_pane_when_editor_hidden", async () => {
  await renderWorkspace();
  openWorkspaceTools();
  fireEvent.click(toggleBtn("Toggle editor"));
  const center = screen.getByTestId("center-editor-pane") as any;
  expect(center.props.hidden).toBe(true);
  const bar = screen.getByTestId("floating-chat-bar") as any;
  expect(bar).toBeDefined();
  expect(center.querySelector('[data-testid="floating-chat-bar"]')).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Minimise to bubble" }));
  expect(Boolean(screen.getByRole("button", { name: "Open AI assistant" }))).toBe(true);
  expect(center.querySelector(".lemma-chat-bubble")).toBeNull();
});

// ---- Jump + flash routing (FEAT-003 Task 3.5) ----
function makeJumpFetchMock() {
  const base = makeForwardSyncFetchMock(() => ({ ok: true, status: 200, json: async () => exactForwardBox }));
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/projects/manuscript-2026/files/main.tex") {
      return { ok: true, status: 200, json: async () => ({ content: "one\ntwo\nthree\nfour\nfive\n", etag: "e1" }) };
    }
    if (url === "/api/projects/manuscript-2026/outline") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          outline: [{ title: "Methods", level: "section", file_path: "main.tex", line_number: 4, children: [] }],
        }),
      };
    }
    if (url.startsWith("/api/projects/manuscript-2026/synctex/inverse")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ file_path: "main.tex", line: 3, column: 1, exact: true, hint: null }),
      };
    }
    return (base as any)(url, init);
  });
}

// Capture the (fake) view the editor creates by observing dispatch, then read the flash decorations off its state.
async function withFlashCapture(run: (flashed: () => number[]) => Promise<void>) {
  const { EditorView } = await import("@codemirror/view");
  const { flashLineField } = await import("../components/flashLine");
  const originalDispatch = (EditorView as any).prototype.dispatch;
  const state: { view: any } = { view: null };
  (EditorView as any).prototype.dispatch = function (tr: any) {
    state.view = this;
    return originalDispatch.call(this, tr);
  };
  const flashed = (): number[] => {
    const set: any = state.view?.state.field(flashLineField, false)?.decorations;
    return (set?.items ?? []).map((r: any) => state.view.state.doc.lineAt(r.from).number);
  };
  try {
    await run(flashed);
  } finally {
    (EditorView as any).prototype.dispatch = originalDispatch;
  }
}

test("test_outline_click_jumps_and_flashes", async () => {
  await withFlashCapture(async (flashed) => {
    await renderAppWithPdf(makeJumpFetchMock());
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Methods \(main\.tex:4\)/ })).toBeDefined();
    });
    expect(flashed()).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: /Methods \(main\.tex:4\)/ }));
    await waitFor(() => {
      expect(flashed()).toEqual([4]);
    });
  });
});

test("test_pdf_double_click_jumps_and_flashes", async () => {
  await withFlashCapture(async (flashed) => {
    await renderAppWithPdf(makeJumpFetchMock());
    fireEvent.doubleClick(screen.getByTestId("pdf-page-surface-1"), { clientX: 10, clientY: 10 });
    await waitFor(() => {
      expect(flashed()).toEqual([3]);
    });
  });
});

test("test_pdf_double_click_snaps_to_heading_and_next_normal_jump_does_not", async () => {
  await withFlashCapture(async (flashed) => {
    const base = makeJumpFetchMock();
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/projects/manuscript-2026/files/main.tex") {
        // Line 1 is the only heading; the synctex hit (line 3) and the outline target (line 4) are body lines.
        return {
          ok: true,
          status: 200,
          json: async () => ({ content: "\\section{A}\nx\nbody\nplain\nfive\n", etag: "e1" }),
        };
      }
      return (base as any)(url, init);
    });
    await renderAppWithPdf(fetchMock);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Methods \(main\.tex:4\)/ })).toBeDefined();
    });

    // PDF double-click (synctex says line 3) snaps up to the heading on line 1.
    fireEvent.doubleClick(screen.getByTestId("pdf-page-surface-1"), { clientX: 10, clientY: 10 });
    await waitFor(() => {
      expect(flashed()).toEqual([1]);
    });

    // A following normal jump must land exactly on its line (4), not reuse the stale snap (=> 1).
    fireEvent.click(screen.getByRole("button", { name: /Methods \(main\.tex:4\)/ }));
    await waitFor(() => {
      expect(flashed()).toEqual([4]);
    });
  });
});

test("test_jump_reshows_hidden_editor_pane", async () => {
  await withFlashCapture(async (flashed) => {
    await renderAppWithPdf(makeJumpFetchMock());
    openWorkspaceTools();
    fireEvent.click(toggleBtn("Toggle editor"));
    expect(screen.getByTestId("center-editor-pane").props.hidden).toBe(true);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Methods \(main\.tex:4\)/ })).toBeDefined();
    });
    fireEvent.click(screen.getByRole("button", { name: /Methods \(main\.tex:4\)/ }));
    await waitFor(() => {
      expect(Boolean(screen.getByTestId("center-editor-pane").props.hidden)).toBe(false);
    });
    await waitFor(() => {
      expect(flashed()).toEqual([4]);
    });
  });
});

test("test_show_in_pdf_flashes_source_line", async () => {
  await withFlashCapture(async (flashed) => {
    await renderAppWithPdf(makeJumpFetchMock());
    expect(flashed()).toEqual([]);
    await clickShowInPdf();
    await waitFor(() => {
      expect(flashed()).toEqual([1]);
    });
  });
});

// ---- Task 3.8: dirty buffer tracking ----

test("test_dirty_buffers_survive_file_switch", async () => {
  await withEditorEditCapture(async (capture) => {
    const base = makeForwardSyncFetchMock(() => ({ ok: true, status: 200, json: async () => exactForwardBox }));
    const restoreBodies: any[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/projects/manuscript-2026/files") {
        return {
          ok: true,
          status: 200,
          json: async () => ({ files: [{ path: "main.tex" }, { path: "other.tex" }] }),
        };
      }
      if (url === "/api/projects/manuscript-2026/files/other.tex") {
        return { ok: true, status: 200, json: async () => ({ content: "other body", etag: "e2" }) };
      }
      if (url === "/api/projects/manuscript-2026/history") {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            snapshots: [{ commit_sha: "abc123", timestamp: "2026-01-01T00:00:00Z", message: "m", trigger: "manual" }],
          }),
        };
      }
      if (url === "/api/projects/manuscript-2026/history/abc123/preview") {
        return { ok: true, status: 200, json: async () => ({
            commit_sha: "abc123",
            changed_files: [],
            unified_diff: "",
            lost_bib_keys: [],
            has_bib_loss_warning: false,
          }) };
      }
      if (url === "/api/projects/manuscript-2026/history/abc123/restore" && method === "POST") {
        restoreBodies.push(JSON.parse(String(init?.body)));
        return { ok: true, status: 200, json: async () => ({}) };
      }
      return base(url, init);
    });
    await renderAppWithPdf(fetchMock);

    await simulateEditorEdit(capture, "hello edited");
    fireEvent.click(await screen.findByRole("button", { name: "other.tex" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some((c: any) => c[0] === "/api/projects/manuscript-2026/files/other.tex")).toBe(true);
    });

    const openHistory = async () => {
      openWorkspaceTools();
      fireEvent.click(screen.getByRole("button", { name: "Version History" }));
      fireEvent.click(await screen.findByRole("button", { name: /Saved manually/ }));
      await waitFor(() => {
        const btn: any = screen.getByRole("button", { name: "Restore this version" });
        expect(Boolean(btn.props.disabled)).toBe(false);
      });
      fireEvent.click(screen.getByRole("button", { name: "Restore this version" }));
      fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    };
    await openHistory();
    await waitFor(() => {
      expect(restoreBodies.length).toBe(1);
    });
    expect(restoreBodies[0].unsaved_buffers).toEqual({ "main.tex": "hello edited" });

    // Entries are cleared once the restore completes.
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    await openHistory();
    await waitFor(() => {
      expect(restoreBodies.length).toBe(2);
    });
    expect(restoreBodies[1].unsaved_buffers).toEqual({});
  });
});

// ---- Task 3.10: history drawer replaces the modal ----

function makeHistoryFetchMock() {
  const base = makeForwardSyncFetchMock(() => ({ ok: true, status: 200, json: async () => exactForwardBox }));
  return vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/projects/manuscript-2026/history") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          snapshots: [{ commit_sha: "abc123", timestamp: "2026-01-01T00:00:00Z", message: "m", trigger: "manual" }],
        }),
      };
    }
    if (url === "/api/projects/manuscript-2026/history/abc123/preview") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          commit_sha: "abc123",
          changed_files: [],
          unified_diff: "",
          lost_bib_keys: [],
          has_bib_loss_warning: false,
        }),
      };
    }
    if (url === "/api/projects/manuscript-2026/history/abc123/restore" && method === "POST") {
      return { ok: true, status: 200, json: async () => ({}) };
    }
    return base(url, init);
  });
}

test("test_history_button_opens_drawer_not_modal", async () => {
  await renderAppWithPdf(makeHistoryFetchMock());
  openWorkspaceTools();
  const btn: any = screen.getByRole("button", { name: "Version History" });
  expect(btn.textContent).toContain("Version history");
  expect(screen.queryByRole("dialog", { name: "Version history" })).toBeNull();

  fireEvent.click(btn);

  const drawer = await screen.findByRole("dialog", { name: "Version history" });
  expect((drawer as any).props.className).toBe("lemma-history-drawer lemma-history-page");
  expect(screen.queryByText("Version History & Restore")).toBeNull();
});

test("test_restore_completed_reloads_files_and_recompiles", async () => {
  const fetchMock = makeHistoryFetchMock();
  await renderAppWithPdf(fetchMock);
  const count = (url: string, method = "GET") =>
    fetchMock.mock.calls.filter((c: any) => c[0] === url && (c[1]?.method ?? "GET") === method).length;
  await waitFor(() => expect(count("/api/projects/manuscript-2026/files")).toBeGreaterThan(0));
  const filesBefore = count("/api/projects/manuscript-2026/files");
  const contentBefore = count("/api/projects/manuscript-2026/files/main.tex");
  const compilesBefore = count("/api/projects/manuscript-2026/compile", "POST");

  openWorkspaceTools();
  fireEvent.click(screen.getByRole("button", { name: "Version History" }));
  fireEvent.click(await screen.findByRole("button", { name: /Saved manually/ }));
  await waitFor(() => {
    const btn: any = screen.getByRole("button", { name: "Restore this version" });
    expect(Boolean(btn.props.disabled)).toBe(false);
  });
  fireEvent.click(screen.getByRole("button", { name: "Restore this version" }));
  fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

  await waitFor(() => {
    expect(count("/api/projects/manuscript-2026/files")).toBe(filesBefore + 1);
    expect(count("/api/projects/manuscript-2026/files/main.tex")).toBe(contentBefore + 1);
    expect(count("/api/projects/manuscript-2026/compile", "POST")).toBe(compilesBefore + 1);
  });
  expect(screen.queryByRole("dialog", { name: "Version history" })).toBeNull();
});

// ---- Review follow-ups: jump/flash correctness, dirty buffer lifecycle ----

// Like withFlashCapture, but exposes the latest (fake) view so tests can read and edit its document.
async function withViewCapture(run: (view: () => any) => Promise<void>) {
  const { EditorView } = await import("@codemirror/view");
  const originalDispatch = (EditorView as any).prototype.dispatch;
  const state: { view: any } = { view: null };
  (EditorView as any).prototype.dispatch = function (tr: any) {
    state.view = this;
    return originalDispatch.call(this, tr);
  };
  try {
    await run(() => state.view);
  } finally {
    (EditorView as any).prototype.dispatch = originalDispatch;
  }
}

function selectionAtLine(line: number) {
  return {
    docChanged: false,
    selectionSet: true,
    state: {
      doc: { toString: () => "", lineAt: () => ({ number: line }) },
      selection: { main: { head: 0, from: 0, to: 0 } },
      sliceDoc: () => "",
    },
  };
}

test("test_forward_sync_flashes_the_synced_line_not_the_stale_cursor_line", async () => {
  await withEditorEditCapture(async (capture) => {
    await withFlashCapture(async (flashed) => {
      await renderAppWithPdf(makeJumpFetchMock());
      // The user moves the caret to line 3 (App's cursorLine is still the default 1).
      capture.fns[capture.fns.length - 1](selectionAtLine(3));
      await clickShowInPdf();
      await waitFor(() => {
        expect(flashed()).toEqual([3]);
      });
    });
  });
});

test("test_failed_forward_sync_does_not_flash_or_jump", async () => {
  await withFlashCapture(async (flashed) => {
    const fetchMock = makeForwardSyncFetchMock(() => ({ ok: false, status: 404, json: async () => ({}) }));
    await renderAppWithPdf(fetchMock);
    await clickShowInPdf();
    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toBe("Compile first");
    });
    expect(flashed()).toEqual([]);
  });
});

test("test_cross_file_jump_waits_for_target_content_before_flashing", async () => {
  await withFlashCapture(async (flashed) => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => {
      release = r;
    });
    const base = makeJumpFetchMock();
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/projects/manuscript-2026/outline") {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            outline: [{ title: "Methods", level: "section", file_path: "other.tex", line_number: 4, children: [] }],
          }),
        };
      }
      if (url === "/api/projects/manuscript-2026/files/other.tex") {
        await gate;
        return { ok: true, status: 200, json: async () => ({ content: "o1\no2\no3\no4\no5\n", etag: "e2" }) };
      }
      return (base as any)(url, init);
    });
    await renderAppWithPdf(fetchMock);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Methods \(other\.tex:4\)/ })).toBeDefined();
    });
    fireEvent.click(screen.getByRole("button", { name: /Methods \(other\.tex:4\)/ }));
    await new Promise((r) => setTimeout(r, 30));
    // other.tex has not loaded: the old document must not be scrolled/flashed.
    expect(flashed()).toEqual([]);
    release();
    await waitFor(() => {
      expect(flashed()).toEqual([4]);
    });
  });
});

function makeTwoFileFetchMock() {
  const base = makeForwardSyncFetchMock(() => ({ ok: true, status: 200, json: async () => exactForwardBox }));
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/projects/manuscript-2026/files") {
      return { ok: true, status: 200, json: async () => ({ files: [{ path: "main.tex" }, { path: "other.tex" }] }) };
    }
    if (url === "/api/projects/manuscript-2026/files/other.tex") {
      return { ok: true, status: 200, json: async () => ({ content: "other body", etag: "e2" }) };
    }
    return (base as any)(url, init);
  });
}

test("test_returning_to_a_dirty_file_shows_its_unsaved_text", async () => {
  await withEditorEditCapture(async (capture) => {
    await withViewCapture(async (view) => {
      const fetchMock = makeTwoFileFetchMock();
      await renderAppWithPdf(fetchMock);
      await waitFor(() => {
        expect(view() === null).toBe(false);
      });
      await simulateEditorEdit(capture, "hello edited");

      fireEvent.click(await screen.findByRole("button", { name: "other.tex" }));
      await waitFor(() => {
        expect(view().state.doc.toString()).toBe("other body");
      });
      fireEvent.click(screen.getByRole("button", { name: "main.tex" }));
      // The refetched saved text ("hello") must not replace the unsaved buffer kept by the app.
      await waitFor(() => {
        expect(view().state.doc.toString()).toBe("hello edited");
      });
    });
  });
});

test("test_restore_reloads_editor_discarding_unsaved_text", async () => {
  await withEditorEditCapture(async (capture) => {
    await withViewCapture(async (view) => {
      const fetchMock = makeHistoryFetchMock();
      await renderAppWithPdf(fetchMock);
      await waitFor(() => {
        expect(view() === null).toBe(false);
      });
      view().dispatch({ changes: { from: 0, to: 5, insert: "typed over" } });
      await simulateEditorEdit(capture, "typed over");

      openWorkspaceTools();
      fireEvent.click(screen.getByRole("button", { name: "Version History" }));
      fireEvent.click(await screen.findByRole("button", { name: /Saved manually/ }));
      await waitFor(() => {
        const btn: any = screen.getByRole("button", { name: "Restore this version" });
        expect(Boolean(btn.props.disabled)).toBe(false);
      });
      fireEvent.click(screen.getByRole("button", { name: "Restore this version" }));
      fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

      // The restored file content equals the previously saved one ("hello"): the editor must still reload it.
      await waitFor(() => {
        expect(view().state.doc.toString()).toBe("hello");
      });
    });
  });
});

test("test_switching_project_resets_stale_flag_and_dirty_buffers", async () => {
  await withEditorEditCapture(async (capture) => {
    const base = makeHistoryFetchMock();
    const restoreBodies: any[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/projects" && method === "POST") {
        return { ok: true, status: 201, json: async () => ({ project_id: "new-paper", name: "New Paper", root_tex: "main.tex" }) };
      }
      if (url.startsWith("/api/projects/new-paper/files/")) {
        return { ok: true, status: 200, json: async () => ({ content: "new paper", etag: "n1" }) };
      }
      if (url === "/api/projects/new-paper/history/abc123/restore" && method === "POST") {
        restoreBodies.push(JSON.parse(String(init?.body)));
        return { ok: true, status: 200, json: async () => ({}) };
      }
      if (url.startsWith("/api/projects/new-paper/history")) {
        return (base as any)(url.replace("/new-paper/", "/manuscript-2026/"), init);
      }
      if (url.startsWith("/api/projects/new-paper/")) {
        return { ok: false, status: 404, json: async () => ({}) };
      }
      return (base as any)(url, init);
    });
    await renderAppWithPdf(fetchMock);
    await simulateEditorEdit(capture, "hello edited");
    await waitFor(() => {
      expect(stalePane().getAttribute("data-stale")).toBe("true");
    });

    fireEvent.click(await screen.findByRole("button", { name: "New project" }));
    fireEvent.change(screen.getByLabelText("Project name"), { target: { value: "New Paper" } });
    fireEvent.click(screen.getByRole("button", { name: /create/i }));
    await waitFor(() => {
      expect((screen.getByLabelText("Select Project") as HTMLSelectElement).value).toBe("new-paper");
    });
    await waitFor(() => {
      expect(stalePane().getAttribute("data-stale")).toBe(null);
    });

    openWorkspaceTools();
    fireEvent.click(screen.getByRole("button", { name: "Version History" }));
    fireEvent.click(await screen.findByRole("button", { name: /Saved manually/ }));
    await waitFor(() => {
      const btn: any = screen.getByRole("button", { name: "Restore this version" });
      expect(Boolean(btn.props.disabled)).toBe(false);
    });
    fireEvent.click(screen.getByRole("button", { name: "Restore this version" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    await waitFor(() => {
      expect(restoreBodies.length).toBe(1);
    });
    expect(restoreBodies[0].unsaved_buffers).toEqual({});
  });
});

test("test_review_studio_jump_flashes_the_target_line", async () => {
  await withFlashCapture(async (flashed) => {
    const base = makeJumpFetchMock();
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/projects/manuscript-2026/reviews") {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            reports: [{ report_id: "r1", snapshot_sha: "s1", timestamp: "2026-01-01T00:00:00Z", issue_count: 1 }],
          }),
        };
      }
      if (url === "/api/projects/manuscript-2026/reviews/r1") {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            issues: [
              {
                issue_id: "iss_1",
                reviewer_persona: "methodology_critic",
                model: "a/model",
                severity: "major",
                file_path: "main.tex",
                original_line: 3,
                resolved_line: 3,
                page_number: 1,
                quoted_anchor: "three",
                title: "Issue",
                critique: "Needs work",
                stale: false,
              },
            ],
          }),
        };
      }
      return (base as any)(url, init);
    });
    await renderAppWithPdf(fetchMock);
    openWorkspaceTools();
    fireEvent.click(screen.getByRole("button", { name: "Review Studio" }));
    await waitFor(() => {
      expect(screen.getByTestId("jump-line-btn-iss_1")).toBeDefined();
    });
    fireEvent.click(screen.getByTestId("jump-line-btn-iss_1"));
    await waitFor(() => {
      expect(flashed()).toEqual([3]);
    });
  });
});

function makeAgentRunAppFetchMock(options: {
  onEventsRequest?: (controller: ReadableStreamDefaultController<Uint8Array>, signal?: AbortSignal | null) => void;
  onStopRequest?: (url: string) => void;
}) {
  if (typeof Range !== "undefined" && !Range.prototype.getClientRects) {
    Range.prototype.getClientRects = (() => ({
      length: 0,
      item: () => null,
      [Symbol.iterator]: function* () {},
    })) as unknown as () => DOMRectList;
    Range.prototype.getBoundingClientRect = () =>
      ({ x: 0, y: 0, width: 0, height: 0, top: 0, left: 0, bottom: 0, right: 0, toJSON: () => ({}) }) as DOMRect;
  }
  const encoder = new TextEncoder();
  return vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/auth/status") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          authenticated: true,
          csrf_token: "csrf-abc",
          insecure_http_warning: false,
          privacy_consent_accepted: true,
        }),
      };
    }
    if (url === "/api/projects") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          projects: [{ project_id: "manuscript-2026", name: "Manuscript", root_tex: "main.tex" }],
        }),
      };
    }
    if (url === "/api/models/enabled") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ models: [{ id: "m1", name: "Model 1" }] }),
      };
    }
    if (url === "/api/projects/manuscript-2026/files") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          files: [
            { path: "main.tex", is_dir: false },
            { path: "intro.tex", is_dir: false },
          ],
        }),
      };
    }
    if (url === "/api/projects/manuscript-2026/files/main.tex") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ content: "Hello world\nSecond line", etag: "etag-1" }),
      };
    }
    if (url === "/api/projects/manuscript-2026/files/intro.tex") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ content: "Intro text", etag: "etag-2" }),
      };
    }
    if (url === "/api/projects/manuscript-2026/pdf") {
      return { ok: false, status: 404, json: async () => ({ detail: "Not found" }) };
    }
    if (url === "/api/projects/manuscript-2026/outline") {
      return { ok: true, status: 200, json: async () => ({ outline: [] }) };
    }
    if (url === "/api/projects/manuscript-2026/chats") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          chats: [
            { chat_id: "chat_a", title: "Chat A", updated_at: "2026-11-14T12:00:00Z" },
            { chat_id: "chat_b", title: "Chat B", updated_at: "2026-11-14T11:00:00Z" },
          ],
        }),
      };
    }
    if (url === "/api/projects/manuscript-2026/chats/chat_a/messages" || url === "/api/projects/manuscript-2026/chats/chat_b/messages") {
      return { ok: true, status: 200, json: async () => ({ messages: [] }) };
    }
    if (url === "/api/ai/runs/active") {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          run: {
            run_id: "run_a",
            project_id: "manuscript-2026",
            chat_id: "chat_a",
            profile_id: "writing",
            status: "running",
          },
        }),
      };
    }
    if (url === "/api/ai/runs/run_a/events") {
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(
            encoder.encode(
              'event: started\ndata: {"type":"started","project_id":"manuscript-2026","chat_id":"chat_a","profile_id":"writing"}\n\n'
            )
          );
          options.onEventsRequest?.(controller, init?.signal);
        },
      });
      return { ok: true, status: 200, body: stream };
    }
    if (url.endsWith("/stop") && method === "POST") {
      options.onStopRequest?.(url);
      return { ok: true, status: 200, json: async () => ({ status: "stopped" }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
}

test("test_background_chat_run_does_not_show_hunks_until_switching_back_to_chat_and_file", async () => {
  cleanup();
  const encoder = new TextEncoder();
  let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
  const fetchMock = makeAgentRunAppFetchMock({
    onEventsRequest: (controller) => {
      streamController = controller;
    },
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);

  // Open project Manuscript from Hub
  const openManuscriptBtn = await screen.findByRole("button", { name: "Open Manuscript" });
  fireEvent.click(openManuscriptBtn);

  await waitFor(() => {
    expect(streamController).not.toBeNull();
  });

  // Switch to Chat B before done
  fireEvent.click(screen.getByRole("tab", { name: "Chats" }));
  const chatBBtn = await screen.findByRole("button", { name: /Chat B/ });
  fireEvent.click(chatBBtn);

  // Emit done with a proposed hunk for Chat A on main.tex
  streamController!.enqueue(
    encoder.encode(
      `event: done\ndata: ${JSON.stringify({
        type: "done",
        project_id: "manuscript-2026",
        chat_id: "chat_a",
        reply: "Updated",
        hunks: [
          {
            hunk_id: "h1",
            file_path: "main.tex",
            base_etag: "etag-1",
            start_line: 1,
            end_line: 1,
            original_text: "Hello world",
            replacement_text: "Hello universe",
            cite_keys: [],
            unknown_cite_keys: [],
            overlap_warnings: [],
            claim_evidence: [],
          },
        ],
      })}\n\n`
    )
  );
  streamController!.close();

  // Wait for background chat activity refresh, and verify Chat B editor has 0 hunks
  await waitFor(() => {
    const chatCalls = fetchMock.mock.calls.filter((c) => c[0] === "/api/projects/manuscript-2026/chats");
    expect(chatCalls.length).toBeGreaterThanOrEqual(2);
  });
  expect(screen.queryByRole("button", { name: "Accept proposed edit" })).toBeNull();

  // Switch back to Chat A on main.tex and verify hunk card appears
  const chatABtn = await screen.findByRole("button", { name: /Chat A/ });
  fireEvent.click(chatABtn);

  const acceptBtn = await screen.findByRole("button", { name: "Accept proposed edit" });
  expect(acceptBtn).toBeTruthy();
});

test("test_switching_to_hub_mid_run_does_not_cancel_active_run", async () => {
  cleanup();
  const encoder = new TextEncoder();
  let streamController: ReadableStreamDefaultController<Uint8Array> | null = null;
  let streamSignal: AbortSignal | null | undefined = null;
  const stopUrls: string[] = [];
  const fetchMock = makeAgentRunAppFetchMock({
    onEventsRequest: (controller, signal) => {
      streamController = controller;
      streamSignal = signal;
    },
    onStopRequest: (url) => {
      stopUrls.push(url);
    },
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);

  const openManuscriptBtn = await screen.findByRole("button", { name: "Open Manuscript" });
  fireEvent.click(openManuscriptBtn);

  await waitFor(() => {
    expect(streamController).not.toBeNull();
  });

  // Switch to Hub mid-run
  fireEvent.click(screen.getByRole("button", { name: /All projects/i }));
  expect(stopUrls).toEqual([]);
  expect(streamSignal?.aborted).toBe(false);

  // Switch back to editor
  fireEvent.click(screen.getByRole("button", { name: /Back to Manuscript/i }));
  expect(stopUrls).toEqual([]);
  expect(streamSignal?.aborted).toBe(false);

  // Finish the active run and verify the hunk appears in the editor
  streamController!.enqueue(
    encoder.encode(
      `event: done\ndata: ${JSON.stringify({
        type: "done",
        project_id: "manuscript-2026",
        chat_id: "chat_a",
        reply: "Updated",
        hunks: [
          {
            hunk_id: "h1",
            file_path: "main.tex",
            base_etag: "etag-1",
            start_line: 1,
            end_line: 1,
            original_text: "Hello world",
            replacement_text: "Hello universe",
            cite_keys: [],
            unknown_cite_keys: [],
            overlap_warnings: [],
            claim_evidence: [],
          },
        ],
      })}\n\n`
    )
  );
  streamController!.close();

  const acceptBtn = await screen.findByRole("button", { name: "Accept proposed edit" });
  expect(acceptBtn).toBeTruthy();
});

test("test_zotero_connected_references_bib_locks_editing_syncs_and_disconnects_without_losing_file", async () => {
  await withViewCapture(async (view) => {
    let bibContent = "@misc{pytorch_cuda,\n  title = {{CUDA} semantics}\n}\n";
    const base = makeForwardSyncFetchMock(() => ({ ok: true, status: 200, json: async () => exactForwardBox }));
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/projects") {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            projects: [
              {
                project_id: "manuscript-2026",
                name: "Manuscript 2026",
                root_tex: "main.tex",
                zotero_collection_key: "COLL_AI",
              },
            ],
          }),
        };
      }
      if (url === "/api/projects/manuscript-2026/files") {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            files: [{ path: "main.tex" }, { path: "references.bib" }],
          }),
        };
      }
      if (url === "/api/projects/manuscript-2026/files/references.bib" && method === "GET") {
        return {
          ok: true,
          status: 200,
          json: async () => ({ content: bibContent, etag: "etag-bib-1" }),
        };
      }
      if (url === "/api/projects/manuscript-2026/zotero/sync" && method === "POST") {
        bibContent += "\n@inproceedings{leviathan_fast_2023,\n  title = {Fast Inference}\n}\n";
        return {
          ok: true,
          status: 200,
          json: async () => ({
            collection_key: "COLL_AI",
            added_bib_count: 1,
            updated_bib_count: 0,
            synced_bib_count: 2,
            synced_pdf_count: 0,
          }),
        };
      }
      if (url === "/api/projects/manuscript-2026/zotero/disconnect" && method === "POST") {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            project_id: "manuscript-2026",
            name: "Manuscript 2026",
            root_tex: "main.tex",
            zotero_collection_key: null,
          }),
        };
      }
      return (base as any)(url, init);
    });

    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Manuscript 2026" }));
    fireEvent.click(await screen.findByRole("button", { name: "references.bib" }));

    await waitFor(() => {
      expect(screen.getByTestId("zotero-sync-banner")).toBeInTheDocument();
      expect(view()?.state.doc.toString()).toContain("pytorch_cuda");
    });

    // Manual editing is locked while Zotero is connected
    view().dispatch({ changes: { from: 0, to: 10, insert: "manual edit" } });
    expect(view().state.doc.toString()).toBe(bibContent);
    expect(screen.getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(true);

    // Clicking Sync pulls new Zotero entries into references.bib
    fireEvent.click(screen.getByRole("button", { name: "Sync" }));
    await waitFor(() => {
      expect(view().state.doc.toString()).toContain("leviathan_fast_2023");
      expect(screen.getByTestId("zotero-sync-banner").textContent).toContain("Zotero: 1 added, 0 updated");
    });

    // Clicking Disconnect Zotero removes the banner, preserves references.bib and its content, and unlocks editing
    fireEvent.click(screen.getByRole("button", { name: "Disconnect Zotero" }));
    await waitFor(() => {
      expect(screen.queryByTestId("zotero-sync-banner")).toBeNull();
    });
    expect(screen.getByRole("button", { name: "references.bib" })).toBeInTheDocument();
    expect(view().state.doc.toString()).toBe(bibContent);
    expect(screen.getByRole("button", { name: "Save" }).hasAttribute("disabled")).toBe(false);

    view().dispatch({ changes: { from: 0, to: bibContent.length, insert: `${bibContent}\n% manual comment\n` } });
    expect(view().state.doc.toString()).toContain("% manual comment");
  });
});
