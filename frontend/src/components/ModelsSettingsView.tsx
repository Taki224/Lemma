import React, { useEffect, useRef, useState } from "react";
import { useEnabledModels } from "../context/EnabledModelsContext";

export interface ModelsSettingsViewProps {
  csrfToken: string;
  onClose?: () => void;
}

interface CatalogModel {
  id: string;
  name: string;
  supports_vision: boolean;
  prompt_price_per_token: number;
  completion_price_per_token: number;
}

const SEARCH_DEBOUNCE_MS = 250;
const SMART_ROUTING_TIMEOUT_MS = 15_000;

function perMillion(pricePerToken: number): string {
  return `$${(pricePerToken * 1_000_000).toFixed(2)}`;
}

export function ModelsSettingsView({
  csrfToken,
  onClose,
}: ModelsSettingsViewProps): React.ReactElement {
  const { models, refresh } = useEnabledModels();
  const [query, setQuery] = useState<string>("");
  const [results, setResults] = useState<CatalogModel[]>([]);
  const [catalogAvailable, setCatalogAvailable] = useState<boolean>(true);
  const [manualId, setManualId] = useState<string>("");
  const [manualVision, setManualVision] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [smartEnabled, setSmartEnabled] = useState<boolean>(false);
  const [smartModel, setSmartModel] = useState<string>("");
  const [smartLoaded, setSmartLoaded] = useState<boolean>(false);
  const [namingEnabled, setNamingEnabled] = useState<boolean>(false);
  const [namingModel, setNamingModel] = useState<string>("");
  const [namingLoaded, setNamingLoaded] = useState<boolean>(false);
  const [localBaseUrl, setLocalBaseUrl] = useState<string>("http://localhost:11434/v1");
  const [localResults, setLocalResults] = useState<CatalogModel[]>([]);
  const [localReachable, setLocalReachable] = useState<boolean | null>(null);
  const [localLoading, setLocalLoading] = useState<boolean>(false);
  const [localManualId, setLocalManualId] = useState<string>("");
  const [localManualVision, setLocalManualVision] = useState<boolean>(false);
  const smartSaveSeq = useRef(0);
  const namingSaveSeq = useRef(0);

  useEffect(() => {
    let active = true;
    const timer = setTimeout(() => {
      fetch(`/api/settings/models/catalog?q=${encodeURIComponent(query)}`)
        .then((res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.json();
        })
        .then((data: { models?: CatalogModel[]; catalog_available?: boolean }) => {
          if (!active) return;
          setResults(Array.isArray(data.models) ? data.models : []);
          setCatalogAvailable(data.catalog_available !== false);
        })
        .catch(() => {
          if (!active) return;
          setResults([]);
          setCatalogAvailable(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [query]);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), SMART_ROUTING_TIMEOUT_MS);
    fetch("/api/settings/smart-routing", { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data: { enabled?: boolean; model?: string }) => {
        if (!active) return;
        setSmartEnabled(Boolean(data.enabled));
        setSmartModel(typeof data.model === "string" ? data.model : "");
        setSmartLoaded(true);
      })
      .catch(() => {
        if (!active) return;
        setErrorMessage("Could not load smart routing settings.");
      });
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, []);

  useEffect(() => {
    let active = true;
    fetch("/api/settings/models/local/config")
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { base_url?: string } | null) => {
        if (!active || !data || typeof data.base_url !== "string") return;
        if (data.base_url.trim()) {
          setLocalBaseUrl(data.base_url);
        }
      })
      .catch(() => {
        // Keep default local base URL if config endpoint is unavailable.
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), SMART_ROUTING_TIMEOUT_MS);
    fetch("/api/settings/chat-naming", { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data: { enabled?: boolean; model?: string }) => {
        if (!active) return;
        setNamingEnabled(Boolean(data.enabled));
        setNamingModel(typeof data.model === "string" ? data.model : "");
        setNamingLoaded(true);
      })
      .catch(() => {
        if (!active) return;
        setNamingLoaded(true);
      });
    return () => {
      active = false;
      clearTimeout(timer);
      controller.abort();
    };
  }, []);

  const persistSmartRouting = async (enabled: boolean, modelId: string) => {
    const seq = ++smartSaveSeq.current;
    const previousEnabled = smartEnabled;
    const previousModel = smartModel;
    setSmartEnabled(enabled);
    setSmartModel(modelId);
    setErrorMessage(null);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), SMART_ROUTING_TIMEOUT_MS);
    try {
      const res = await fetch("/api/settings/smart-routing", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ enabled, model: modelId }),
        signal: controller.signal,
      });
      if (seq !== smartSaveSeq.current) return;
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setSmartEnabled(previousEnabled);
        setSmartModel(previousModel);
        setErrorMessage(err.detail || `Request failed (HTTP ${res.status})`);
      }
    } catch (err: unknown) {
      if (seq !== smartSaveSeq.current) return;
      setSmartEnabled(previousEnabled);
      setSmartModel(previousModel);
      setErrorMessage(err instanceof Error ? err.message : "Request failed");
    } finally {
      clearTimeout(timer);
    }
  };

  const persistChatNaming = async (enabled: boolean, modelId: string) => {
    const seq = ++namingSaveSeq.current;
    const previousEnabled = namingEnabled;
    const previousModel = namingModel;
    setNamingEnabled(enabled);
    setNamingModel(modelId);
    setErrorMessage(null);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), SMART_ROUTING_TIMEOUT_MS);
    try {
      const res = await fetch("/api/settings/chat-naming", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ enabled, model: modelId }),
        signal: controller.signal,
      });
      if (seq !== namingSaveSeq.current) return;
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setNamingEnabled(previousEnabled);
        setNamingModel(previousModel);
        setErrorMessage(err.detail || `Request failed (HTTP ${res.status})`);
      }
    } catch (err: unknown) {
      if (seq !== namingSaveSeq.current) return;
      setNamingEnabled(previousEnabled);
      setNamingModel(previousModel);
      setErrorMessage(err instanceof Error ? err.message : "Request failed");
    } finally {
      clearTimeout(timer);
    }
  };

  const mutate = async (url: string, method: "POST" | "DELETE", body?: unknown): Promise<boolean> => {
    setErrorMessage(null);
    try {
      const res = await fetch(url, {
        method,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setErrorMessage(err.detail || `Request failed (HTTP ${res.status})`);
        return false;
      }
      await refresh();
      return true;
    } catch (err: any) {
      setErrorMessage(err?.message || "Request failed");
      return false;
    }
  };

  const enableFromCatalog = (modelId: string) =>
    mutate("/api/settings/models", "POST", { model_id: modelId });

  const removeModel = (modelId: string) =>
    mutate(`/api/settings/models/${encodeURIComponent(modelId)}`, "DELETE");

  const addManual = async () => {
    const id = manualId.trim();
    if (!id) return;
    const ok = await mutate("/api/settings/models", "POST", {
      model_id: id,
      supports_vision: manualVision,
    });
    if (ok) {
      setManualId("");
      setManualVision(false);
    }
  };

  const fetchLocalCatalog = async () => {
    const trimmedUrl = localBaseUrl.trim().replace(/\/+$/, "");
    if (!trimmedUrl) {
      setErrorMessage("Enter a local server base URL (e.g. http://localhost:11434/v1).");
      return;
    }
    setErrorMessage(null);
    setLocalLoading(true);
    try {
      const cfgRes = await fetch("/api/settings/models/local/config", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ base_url: trimmedUrl }),
      });
      if (!cfgRes.ok) {
        const err = await cfgRes.json().catch(() => ({}));
        setErrorMessage(err.detail || `Request failed (HTTP ${cfgRes.status})`);
        setLocalReachable(false);
        return;
      }
      const res = await fetch(
        `/api/settings/models/local/catalog?base_url=${encodeURIComponent(trimmedUrl)}`
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setErrorMessage(err.detail || `Request failed (HTTP ${res.status})`);
        setLocalReachable(false);
        setLocalResults([]);
        return;
      }
      const data: { models?: CatalogModel[]; reachable?: boolean } = await res.json();
      setLocalResults(Array.isArray(data.models) ? data.models : []);
      setLocalReachable(Boolean(data.reachable));
    } catch (err: unknown) {
      setLocalResults([]);
      setLocalReachable(false);
      setErrorMessage(err instanceof Error ? err.message : "Could not reach local server");
    } finally {
      setLocalLoading(false);
    }
  };

  const enableLocalModel = (modelId: string, supportsVision?: boolean) =>
    mutate("/api/settings/models", "POST", {
      model_id: modelId,
      provider: "local",
      base_url: localBaseUrl.trim().replace(/\/+$/, ""),
      ...(supportsVision !== undefined ? { supports_vision: supportsVision } : {}),
    });

  const addLocalManual = async () => {
    const id = localManualId.trim();
    if (!id) return;
    const ok = await enableLocalModel(id, localManualVision);
    if (ok) {
      setLocalManualId("");
      setLocalManualVision(false);
    }
  };

  const enabledIds = new Set(models.map((m) => m.id));

  return (
    <div className="lemma-review-studio lemma-models-settings" data-testid="models-settings-view">
      <header className="lemma-review-studio-header">
        <div className="lemma-review-studio-title-block">
          <h2>Settings &gt; Models</h2>
          <span className="lemma-review-studio-subtitle">
            Choose which OpenRouter or local models appear in every model selector.
          </span>
        </div>
        {onClose && (
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary"
            onClick={onClose}
            data-testid="close-models-settings-btn"
          >
            Back to Editor
          </button>
        )}
      </header>

      <section className="lemma-settings-card" data-testid="smart-routing-section">
        <h3>Smart routing</h3>
        <p className="lemma-subtext">
          When this is on, the selected model chooses Drafting, Literature, or Proofreading for each prompt.
        </p>
        <div className="lemma-smart-routing-row">
          <label className="lemma-checkbox-label">
            <input
              type="checkbox"
              checked={smartEnabled}
              disabled={!smartLoaded}
              onChange={(e) => {
                const turningOn = e.target.checked;
                const modelIsEnabled = models.some((m) => m.id === smartModel);
                if (turningOn && !modelIsEnabled) {
                  setErrorMessage("Choose an enabled model before turning smart routing on.");
                  return;
                }
                void persistSmartRouting(turningOn, smartModel);
              }}
            />
            <span>Use a model to choose the chat mode</span>
          </label>
          <select
            aria-label="Smart routing model"
            value={smartModel}
            disabled={!smartLoaded || (models.length === 0 && smartModel === "")}
            onChange={(e) => {
              void persistSmartRouting(smartEnabled, e.target.value);
            }}
          >
            {models.length === 0 && smartModel === "" ? (
              <option value="">No models enabled</option>
            ) : (
              <>
                {smartModel === "" && <option value="">Select a model</option>}
                {smartModel !== "" && !models.some((m) => m.id === smartModel) && (
                  <option value={smartModel}>{smartModel} (unavailable)</option>
                )}
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </>
            )}
          </select>
        </div>
      </section>

      <section className="lemma-settings-card" data-testid="chat-naming-section">
        <h3>Chat session naming</h3>
        <p className="lemma-subtext">
          When this is on, the selected model gives each new chat a short name from its initial prompt.
        </p>
        <div className="lemma-smart-routing-row">
          <label className="lemma-checkbox-label">
            <input
              type="checkbox"
              checked={namingEnabled}
              disabled={!namingLoaded}
              onChange={(e) => {
                const turningOn = e.target.checked;
                const modelIsEnabled = models.some((m) => m.id === namingModel);
                if (turningOn && !modelIsEnabled) {
                  setErrorMessage("Choose an enabled model before turning chat session naming on.");
                  return;
                }
                void persistChatNaming(turningOn, namingModel);
              }}
            />
            <span>Use a model to name new chat sessions</span>
          </label>
          <select
            aria-label="Chat naming model"
            value={namingModel}
            disabled={!namingLoaded || (models.length === 0 && namingModel === "")}
            onChange={(e) => {
              void persistChatNaming(namingEnabled, e.target.value);
            }}
          >
            {models.length === 0 && namingModel === "" ? (
              <option value="">No models enabled</option>
            ) : (
              <>
                {namingModel === "" && <option value="">Select a model</option>}
                {namingModel !== "" && !models.some((m) => m.id === namingModel) && (
                  <option value={namingModel}>{namingModel} (unavailable)</option>
                )}
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}
                  </option>
                ))}
              </>
            )}
          </select>
        </div>
      </section>

      {errorMessage && (
        <div className="lemma-error-banner" role="alert" data-testid="settings-error">
          {errorMessage}
        </div>
      )}

      <section className="lemma-settings-card" data-testid="enabled-models-section">
        <h3>Enabled models</h3>
        {models.length === 0 ? (
          <p className="lemma-empty-state" data-testid="no-models-notice">
            No models enabled. Search the catalog or add a model ID below.
          </p>
        ) : (
          <ul className="lemma-model-list" data-testid="enabled-models-list">
            {models.map((m) => (
              <li key={m.id} className="lemma-model-row" data-testid={`enabled-model-${m.id}`}>
                <div className="lemma-model-row-main">
                  <span className="lemma-model-name">{m.name}</span>
                  <code className="lemma-model-id">{m.id}</code>
                  <span className="lemma-model-price">
                    {m.provider === "local"
                      ? `Local (${m.base_url || "custom endpoint"})`
                      : `${perMillion(m.prompt_price_per_token)} in / ${perMillion(m.completion_price_per_token)} out per 1M tokens`}
                  </span>
                </div>
                {m.provider === "local" && (
                  <span className="lemma-model-badge lemma-badge-info" data-testid={`badge-local-${m.id}`}>
                    local
                  </span>
                )}
                {m.supports_vision && (
                  <span className="lemma-model-badge lemma-badge-success" data-testid={`badge-vision-${m.id}`}>
                    vision
                  </span>
                )}
                {!m.verified && (
                  <span className="lemma-model-badge lemma-badge-warning" data-testid={`badge-unverified-${m.id}`}>
                    unverified
                  </span>
                )}
                <button
                  type="button"
                  className="lemma-btn lemma-btn-secondary lemma-btn-xs"
                  aria-label={`Remove ${m.id}`}
                  onClick={() => removeModel(m.id)}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="lemma-settings-card" data-testid="catalog-section">
        <h3>Add from OpenRouter catalog</h3>
        <input
          type="text"
          className="lemma-settings-input"
          aria-label="Search model catalog"
          placeholder="Search models (e.g. claude, gpt-4o)"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {!catalogAvailable && (
          <p className="lemma-error-banner" role="alert" data-testid="catalog-error">
            The model catalog is unavailable right now. You can still add a model by ID below.
          </p>
        )}
        <ul className="lemma-model-list lemma-model-list-scroll" data-testid="catalog-results">
          {results.map((m) => (
            <li key={m.id} className="lemma-model-row" data-testid={`catalog-result-${m.id}`}>
              <div className="lemma-model-row-main">
                <span className="lemma-model-name">{m.name}</span>
                <code className="lemma-model-id">{m.id}</code>
              </div>
              {m.supports_vision && <span className="lemma-model-badge lemma-badge-success">vision</span>}
              {enabledIds.has(m.id) ? (
                <span className="lemma-model-enabled-tag" data-testid={`catalog-enabled-${m.id}`}>Enabled</span>
              ) : (
                <button
                  type="button"
                  className="lemma-btn lemma-btn-primary lemma-btn-xs"
                  aria-label={`Enable ${m.id}`}
                  onClick={() => enableFromCatalog(m.id)}
                >
                  Enable
                </button>
              )}
            </li>
          ))}
        </ul>
      </section>

      <section className="lemma-settings-card" data-testid="manual-entry-section">
        <h3>Add by model ID</h3>
        <div className="lemma-model-manual-row">
        <input
          type="text"
          className="lemma-settings-input"
          aria-label="Model ID"
          placeholder="provider/model-name"
          value={manualId}
          onChange={(e) => setManualId(e.target.value)}
        />
        <label className="lemma-checkbox-label">
          <input
            type="checkbox"
            aria-label="Vision-capable"
            checked={manualVision}
            onChange={(e) => setManualVision(e.target.checked)}
          />
          Vision-capable
        </label>
        <button
          type="button"
          className="lemma-btn lemma-btn-primary"
          aria-label="Add Model"
          onClick={addManual}
        >
          Add
        </button>
        </div>
      </section>

      <section className="lemma-settings-card" data-testid="local-models-section">
        <h3>Local model (OpenAI-compatible)</h3>
        <p className="lemma-subtext">
          Connect to a local server (Ollama <code>http://localhost:11434/v1</code>, LM Studio{" "}
          <code>http://localhost:1234/v1</code>, or llama.cpp). Local models run without OpenRouter.
        </p>
        <div className="lemma-model-manual-row">
          <input
            type="text"
            className="lemma-settings-input"
            aria-label="Local model base URL"
            placeholder="http://localhost:11434/v1"
            value={localBaseUrl}
            onChange={(e) => setLocalBaseUrl(e.target.value)}
          />
          <button
            type="button"
            className="lemma-btn lemma-btn-secondary"
            aria-label="Fetch local models"
            disabled={localLoading}
            onClick={() => void fetchLocalCatalog()}
          >
            {localLoading ? "Checking..." : "Fetch models"}
          </button>
        </div>
        {localReachable === false && (
          <p className="lemma-error-banner" role="alert" data-testid="local-catalog-error">
            Could not reach local server at {localBaseUrl}. Make sure it is running, or add a local model ID below.
          </p>
        )}
        {localReachable === true && localResults.length === 0 && (
          <p className="lemma-subtext" data-testid="local-catalog-empty">
            Connected, but no models were listed. Add a model ID below.
          </p>
        )}
        {localResults.length > 0 && (
          <ul className="lemma-model-list lemma-model-list-scroll" data-testid="local-catalog-results">
            {localResults.map((m) => (
              <li key={m.id} className="lemma-model-row" data-testid={`local-catalog-result-${m.id}`}>
                <div className="lemma-model-row-main">
                  <span className="lemma-model-name">{m.name}</span>
                  <code className="lemma-model-id">{m.id}</code>
                </div>
                {m.supports_vision && <span className="lemma-model-badge lemma-badge-success">vision</span>}
                {enabledIds.has(m.id) ? (
                  <span className="lemma-model-enabled-tag" data-testid={`local-catalog-enabled-${m.id}`}>Enabled</span>
                ) : (
                  <button
                    type="button"
                    className="lemma-btn lemma-btn-primary lemma-btn-xs"
                    aria-label={`Enable local ${m.id}`}
                    onClick={() => enableLocalModel(m.id)}
                  >
                    Enable
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        <div className="lemma-model-manual-row">
          <input
            type="text"
            className="lemma-settings-input"
            aria-label="Local model ID"
            placeholder="e.g. llama3.2:latest"
            value={localManualId}
            onChange={(e) => setLocalManualId(e.target.value)}
          />
          <label className="lemma-checkbox-label">
            <input
              type="checkbox"
              aria-label="Local model vision-capable"
              checked={localManualVision}
              onChange={(e) => setLocalManualVision(e.target.checked)}
            />
            Vision-capable
          </label>
          <button
            type="button"
            className="lemma-btn lemma-btn-primary"
            aria-label="Add local model"
            onClick={() => void addLocalManual()}
          >
            Add local
          </button>
        </div>
      </section>
    </div>
  );
}

export default ModelsSettingsView;
