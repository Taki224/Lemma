import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { EnabledModelsContext, type EnabledModelInfo } from "../context/EnabledModelsContext";
import { ModelsSettingsView } from "../components/ModelsSettingsView";

function makeModel(id: string, overrides: Partial<EnabledModelInfo> = {}): EnabledModelInfo {
  return {
    id,
    name: `Name of ${id}`,
    supports_vision: false,
    verified: true,
    prompt_price_per_token: 0.000003,
    completion_price_per_token: 0.000015,
    ...overrides,
  };
}

function catalogEntry(id: string, supportsVision = false) {
  return {
    id,
    name: `Catalog ${id}`,
    context_length: 200000,
    prompt_price_per_token: 0.000003,
    completion_price_per_token: 0.000015,
    supports_vision: supportsVision,
  };
}

function renderView(models: EnabledModelInfo[], refresh = vi.fn(async () => {})): any {
  render(
    <EnabledModelsContext.Provider value={{ models, loading: false, refresh }}>
      <ModelsSettingsView csrfToken="csrf-xyz" />
    </EnabledModelsContext.Provider>
  );
  return refresh;
}

function mockFetch(
  catalog: { models: any[]; catalog_available: boolean },
  smart: { enabled: boolean; model: string } = { enabled: false, model: "" }
): any {
  const fn = vi.fn().mockImplementation((url: string, options?: any) => {
    const method = options?.method ?? "GET";
    if (String(url) === "/api/settings/models/local/config" && method === "GET") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ base_url: "http://localhost:11434/v1" }),
      });
    }
    if (String(url) === "/api/settings/models/local/config" && method === "PUT") {
      const body = options?.body ? JSON.parse(options.body) : {};
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
      });
    }
    if (String(url).startsWith("/api/settings/models/local/catalog") && method === "GET") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            models: [catalogEntry("llama3.2:latest")],
            reachable: true,
            base_url: "http://localhost:11434/v1",
          }),
      });
    }
    if (String(url) === "/api/settings/smart-routing" && method === "GET") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(smart),
      });
    }
    if (String(url) === "/api/settings/smart-routing" && method === "PUT") {
      const body = options?.body ? JSON.parse(options.body) : {};
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
      });
    }
    if (String(url) === "/api/settings/chat-naming" && method === "GET") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ enabled: false, model: "" }),
      });
    }
    if (String(url) === "/api/settings/chat-naming" && method === "PUT") {
      const body = options?.body ? JSON.parse(options.body) : {};
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
      });
    }
    if (String(url) === "/api/settings/keys" && method === "GET") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ github: [], zotero: [] }),
      });
    }
    if (String(url).startsWith("/api/settings/models/catalog") && method === "GET") {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(catalog) });
    }
    if (method === "POST") {
      return Promise.resolve({ ok: true, status: 201, json: () => Promise.resolve({}) });
    }
    if (method === "DELETE") {
      return Promise.resolve({ ok: true, status: 204, json: () => Promise.resolve({}) });
    }
    return Promise.reject(new Error(`Unhandled fetch: ${url}`));
  });
  global.fetch = fn;
  return fn;
}

function callsWithMethod(fetchMock: any, method: string): any[][] {
  return fetchMock.mock.calls.filter((c: any[]) => c[1]?.method === method);
}

describe("ModelsSettingsView", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    cleanup();
    global.fetch = originalFetch;
  });

  it("lists enabled models with badges", () => {
    mockFetch({ models: [], catalog_available: true });
    renderView([
      makeModel("a/vision", { supports_vision: true }),
      makeModel("b/manual", { verified: false }),
    ]);

    const list = screen.getByTestId("enabled-models-list");
    expect(list.textContent).toContain("Name of a/vision");
    expect(list.textContent).toContain("a/vision");
    expect(list.textContent).toContain("$3.00");
    expect(list.textContent).toContain("$15.00");
    expect(Boolean(screen.getByTestId("badge-vision-a/vision"))).toBe(true);
    expect(screen.queryByTestId("badge-vision-b/manual")).toBeNull();
    expect(Boolean(screen.getByTestId("badge-unverified-b/manual"))).toBe(true);
    expect(screen.queryByTestId("badge-unverified-a/vision")).toBeNull();
    expect(screen.queryByTestId("no-models-notice")).toBeNull();
  });

  it("search shows results and enabling calls POST with csrf then refreshes", async () => {
    const fetchMock = mockFetch({
      models: [catalogEntry("x/new"), catalogEntry("a/existing")],
      catalog_available: true,
    });
    const refresh = renderView([makeModel("a/existing")]);

    fireEvent.change(screen.getByLabelText("Search model catalog"), {
      target: { value: "new" },
    });

    await waitFor(() => {
      expect(Boolean(screen.getByTestId("catalog-result-x/new"))).toBe(true);
    });
    expect(
      fetchMock.mock.calls.some((c: any[]) => c[0] === "/api/settings/models/catalog?q=new")
    ).toBe(true);
    expect(Boolean(screen.getByTestId("catalog-enabled-a/existing"))).toBe(true);
    expect(screen.queryByLabelText("Enable a/existing")).toBeNull();

    fireEvent.click(screen.getByLabelText("Enable x/new"));

    await waitFor(() => {
      expect(refresh).toHaveBeenCalledTimes(1);
    });
    const posts = callsWithMethod(fetchMock, "POST");
    expect(posts.length).toBe(1);
    expect(posts[0][0]).toBe("/api/settings/models");
    expect(posts[0][1].headers["X-CSRF-Token"]).toBe("csrf-xyz");
    expect(JSON.parse(posts[0][1].body)).toEqual({ model_id: "x/new" });
  });

  it("remove calls DELETE with encoded id then refreshes", async () => {
    const fetchMock = mockFetch({ models: [], catalog_available: true });
    const refresh = renderView([makeModel("a/model")]);

    fireEvent.click(screen.getByLabelText("Remove a/model"));

    await waitFor(() => {
      expect(refresh).toHaveBeenCalledTimes(1);
    });
    const deletes = callsWithMethod(fetchMock, "DELETE");
    expect(deletes.length).toBe(1);
    expect(deletes[0][0]).toBe("/api/settings/models/a%2Fmodel");
    expect(deletes[0][1].headers["X-CSRF-Token"]).toBe("csrf-xyz");
  });

  it("manual id entry posts model_id and vision flag", async () => {
    const fetchMock = mockFetch({ models: [], catalog_available: true });
    const refresh = renderView([]);

    fireEvent.change(screen.getByLabelText("Model ID"), {
      target: { value: "custom/model-1" },
    });
    // The test shim toggles checkboxes through change events, not clicks.
    fireEvent.change(screen.getByLabelText("Vision-capable"), { target: { checked: true } });
    fireEvent.click(screen.getByLabelText("Add Model"));

    await waitFor(() => {
      expect(refresh).toHaveBeenCalledTimes(1);
    });
    const posts = callsWithMethod(fetchMock, "POST");
    expect(posts.length).toBe(1);
    expect(posts[0][1].headers["X-CSRF-Token"]).toBe("csrf-xyz");
    expect(JSON.parse(posts[0][1].body)).toEqual({
      model_id: "custom/model-1",
      supports_vision: true,
    });
  });

  it("catalog unavailable shows error but manual entry works", async () => {
    const fetchMock = mockFetch({ models: [], catalog_available: false });
    const refresh = renderView([]);

    await waitFor(() => {
      expect(Boolean(screen.getByTestId("catalog-error"))).toBe(true);
    });

    fireEvent.change(screen.getByLabelText("Model ID"), {
      target: { value: "custom/model-2" },
    });
    fireEvent.click(screen.getByLabelText("Add Model"));

    await waitFor(() => {
      expect(refresh).toHaveBeenCalledTimes(1);
    });
    expect(JSON.parse(callsWithMethod(fetchMock, "POST")[0][1].body)).toEqual({
      model_id: "custom/model-2",
      supports_vision: false,
    });
  });

  it("empty list shows notice", () => {
    mockFetch({ models: [], catalog_available: true });
    renderView([]);

    expect(Boolean(screen.getByTestId("no-models-notice"))).toBe(true);
    expect(screen.getByTestId("no-models-notice").textContent).toContain("No models enabled");
    expect(screen.queryByTestId("enabled-models-list")).toBeNull();
  });

  it("failed mutation shows an error and does not refresh", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, options?: any) => {
      if (options?.method === "POST") {
        return Promise.resolve({
          ok: false,
          status: 422,
          json: () => Promise.resolve({ detail: "Invalid model id" }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ models: [], catalog_available: true }),
      });
    });
    global.fetch = fetchMock;
    const refresh = renderView([]);

    fireEvent.change(screen.getByLabelText("Model ID"), { target: { value: "bad id" } });
    fireEvent.click(screen.getByLabelText("Add Model"));

    await waitFor(() => {
      expect(screen.getByTestId("settings-error").textContent).toContain("Invalid model id");
    });
    expect(refresh).toHaveBeenCalledTimes(0);
  });

  it("saves smart routing with the model chosen beside the switch", async () => {
    const fetchMock = mockFetch(
      { models: [], catalog_available: true },
      { enabled: false, model: "a/model" }
    );
    renderView([makeModel("a/model"), makeModel("b/model")]);

    const toggle = screen.getByLabelText("Use a model to choose the chat mode");
    await waitFor(() => {
      expect(toggle.hasAttribute("disabled")).toBe(false);
    });

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(callsWithMethod(fetchMock, "PUT").length).toBe(1);
    });
    expect(JSON.parse(callsWithMethod(fetchMock, "PUT")[0][1].body)).toEqual({
      enabled: true,
      model: "a/model",
    });
    expect(callsWithMethod(fetchMock, "PUT")[0][1].headers["X-CSRF-Token"]).toBe("csrf-xyz");

    fireEvent.change(screen.getByLabelText("Smart routing model"), {
      target: { value: "b/model" },
    });
    await waitFor(() => {
      expect(callsWithMethod(fetchMock, "PUT").length).toBe(2);
    });
    expect(JSON.parse(callsWithMethod(fetchMock, "PUT")[1][1].body)).toEqual({
      enabled: true,
      model: "b/model",
    });
  });

  it("keeps smart routing disabled when the settings request fails", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url) === "/api/settings/smart-routing") {
        return Promise.resolve({
          ok: false,
          status: 500,
          json: () => Promise.resolve({ detail: "unavailable" }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ models: [], catalog_available: true }),
      });
    });
    global.fetch = fetchMock;
    renderView([makeModel("a/model")]);

    await waitFor(() => {
      expect(screen.getByTestId("settings-error").textContent).toContain(
        "Could not load smart routing settings."
      );
    });
    expect(
      screen.getByLabelText("Use a model to choose the chat mode").hasAttribute("disabled")
    ).toBe(true);
    expect(callsWithMethod(fetchMock, "PUT").length).toBe(0);
  });

  it("restores the switch when saving smart routing fails", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string, options?: any) => {
      const method = options?.method ?? "GET";
      if (String(url) === "/api/settings/models/local/config" && method === "GET") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ base_url: "http://localhost:11434/v1" }),
      });
    }
    if (String(url) === "/api/settings/models/local/config" && method === "PUT") {
      const body = options?.body ? JSON.parse(options.body) : {};
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(body),
      });
    }
    if (String(url).startsWith("/api/settings/models/local/catalog") && method === "GET") {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            models: [catalogEntry("llama3.2:latest")],
            reachable: true,
            base_url: "http://localhost:11434/v1",
          }),
      });
    }
    if (String(url) === "/api/settings/smart-routing" && method === "GET") {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ enabled: false, model: "a/model" }),
        });
      }
      if (String(url) === "/api/settings/smart-routing" && method === "PUT") {
        return Promise.resolve({
          ok: false,
          status: 500,
          json: () => Promise.resolve({ detail: "Could not save smart routing" }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ models: [], catalog_available: true }),
      });
    });
    global.fetch = fetchMock;
    renderView([makeModel("a/model")]);

    const toggle = screen.getByLabelText("Use a model to choose the chat mode") as HTMLInputElement;
    await waitFor(() => {
      expect(toggle.hasAttribute("disabled")).toBe(false);
    });
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(screen.getByTestId("settings-error").textContent).toContain(
        "Could not save smart routing"
      );
    });
    expect(toggle.checked).toBe(false);
  });

  it("shows a saved routing model that is no longer enabled", async () => {
    const fetchMock = mockFetch(
      { models: [], catalog_available: true },
      { enabled: false, model: "gone/model" }
    );
    renderView([makeModel("a/model")]);

    const select = screen.getByLabelText("Smart routing model") as HTMLSelectElement;
    await waitFor(() => {
      expect(select.hasAttribute("disabled")).toBe(false);
    });
    expect(select.value).toBe("gone/model");

    fireEvent.click(screen.getByLabelText("Use a model to choose the chat mode"));
    expect(callsWithMethod(fetchMock, "PUT").length).toBe(0);
    expect(screen.getByTestId("settings-error").textContent).toContain(
      "Choose an enabled model"
    );
  });

  it("shows local badge and base URL for local models and supports adding local models", async () => {
    const fetchMock = mockFetch({ models: [], catalog_available: true });
    const refresh = renderView([
      makeModel("llama3.2:latest", {
        provider: "local",
        base_url: "http://localhost:11434/v1",
        prompt_price_per_token: 0,
        completion_price_per_token: 0,
      }),
    ]);

    const list = screen.getByTestId("enabled-models-list");
    expect(Boolean(screen.getByTestId("badge-local-llama3.2:latest"))).toBe(true);
    expect(list.textContent).toContain("Local (http://localhost:11434/v1)");

    fireEvent.change(screen.getByLabelText("Local model ID"), {
      target: { value: "qwen2.5:14b" },
    });
    fireEvent.change(screen.getByLabelText("Local model vision-capable"), {
      target: { checked: true },
    });
    fireEvent.click(screen.getByLabelText("Add local model"));

    await waitFor(() => {
      expect(refresh).toHaveBeenCalledTimes(1);
    });
    const posts = callsWithMethod(fetchMock, "POST");
    expect(posts.length).toBe(1);
    expect(JSON.parse(posts[0][1].body)).toEqual({
      model_id: "qwen2.5:14b",
      provider: "local",
      base_url: "http://localhost:11434/v1",
      supports_vision: true,
    });
  });

  it("saves chat session naming toggle and model selection", async () => {
    const fetchMock = mockFetch({ models: [], catalog_available: true });
    renderView([makeModel("a/model"), makeModel("b/model")]);

    const select = screen.getByLabelText("Chat naming model") as HTMLSelectElement;
    const toggle = screen.getByLabelText("Use a model to name new chat sessions") as HTMLInputElement;
    await waitFor(() => {
      expect(toggle.hasAttribute("disabled")).toBe(false);
    });

    fireEvent.change(select, { target: { value: "b/model" } });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (c: any[]) =>
            c[0] === "/api/settings/chat-naming" &&
            c[1]?.method === "PUT" &&
            JSON.parse(c[1].body).model === "b/model"
        )
      ).toBe(true);
    });

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (c: any[]) =>
            c[0] === "/api/settings/chat-naming" &&
            c[1]?.method === "PUT" &&
            JSON.parse(c[1].body).enabled === true &&
            JSON.parse(c[1].body).model === "b/model"
        )
      ).toBe(true);
    });
  });

  it("adds and removes named GitHub and Zotero keys", async () => {
    const keysState = { github: [] as Array<{ name: string }>, zotero: [] as Array<{ name: string }> };
    const fetchMock = vi.fn().mockImplementation((url: string, options?: any) => {
      const method = options?.method ?? "GET";
      if (String(url) === "/api/settings/keys" && method === "GET") {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(keysState),
        });
      }
      if (String(url) === "/api/settings/keys" && method === "POST") {
        const body = JSON.parse(options.body);
        if (body.provider === "github") {
          keysState.github = [...keysState.github, { name: body.name }];
        } else {
          keysState.zotero = [...keysState.zotero, { name: body.name }];
        }
        return Promise.resolve({
          ok: true,
          status: 201,
          json: () => Promise.resolve(keysState),
        });
      }
      if (String(url).startsWith("/api/settings/keys/") && method === "DELETE") {
        const parts = String(url).split("/");
        const name = decodeURIComponent(parts[parts.length - 1]);
        const provider = parts[parts.length - 2];
        if (provider === "github") {
          keysState.github = keysState.github.filter((k) => k.name !== name);
        } else {
          keysState.zotero = keysState.zotero.filter((k) => k.name !== name);
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(keysState),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ models: [], catalog_available: true }),
      });
    });
    global.fetch = fetchMock;

    renderView([makeModel("a/model")]);

    fireEvent.change(screen.getByLabelText("Key type"), { target: { value: "github" } });
    fireEvent.change(screen.getByLabelText("Key name"), { target: { value: "Personal GitHub" } });
    fireEvent.change(screen.getByLabelText("Key value"), { target: { value: "ghp_secret12345" } });
    fireEvent.click(screen.getByLabelText("Add Key"));

    await waitFor(() => {
      expect(Boolean(screen.getByTestId("saved-key-github-Personal GitHub"))).toBe(true);
    });

    fireEvent.change(screen.getByLabelText("Key type"), { target: { value: "zotero" } });
    fireEvent.change(screen.getByLabelText("Key name"), { target: { value: "Lab Zotero" } });
    fireEvent.change(screen.getByLabelText("Key value"), { target: { value: "zot_secret_key" } });
    fireEvent.click(screen.getByLabelText("Add Key"));

    await waitFor(() => {
      expect(Boolean(screen.getByTestId("saved-key-zotero-Lab Zotero"))).toBe(true);
    });

    fireEvent.click(screen.getByLabelText("Remove GitHub key Personal GitHub"));
    await waitFor(() => {
      expect(screen.queryByTestId("saved-key-github-Personal GitHub")).toBeNull();
    });
  });

  it("changes the active project's LaTeX compiler under Advanced settings", async () => {
    installCatalogFetch([]);
    const onUpdateTexEngine = vi.fn(async () => null);
    render(
      <EnabledModelsProvider initialModels={[makeModel("a/model")]}>
        <ModelsSettingsView
          csrfToken="csrf-tok"
          activeProject={{
            project_id: "manuscript-2026",
            name: "Quantum Paper",
            root_tex: "main.tex",
            tex_engine: "pdflatex",
          }}
          onUpdateTexEngine={onUpdateTexEngine}
        />
      </EnabledModelsProvider>
    );

    expect(screen.getByTestId("advanced-settings-section")).toBeTruthy();
    const select = screen.getByLabelText("LaTeX compiler") as HTMLSelectElement;
    expect(select.value).toBe("pdflatex");

    fireEvent.change(select, { target: { value: "xelatex" } });
    await waitFor(() => {
      expect(onUpdateTexEngine).toHaveBeenCalledWith("xelatex");
    });
  });
});
