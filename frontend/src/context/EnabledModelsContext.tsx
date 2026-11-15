import React, { createContext, useCallback, useContext, useEffect, useState } from "react";

export interface EnabledModelInfo {
  id: string;
  name: string;
  supports_vision: boolean;
  verified: boolean;
  prompt_price_per_token: number;
  completion_price_per_token: number;
  provider?: "openrouter" | "local";
  base_url?: string;
}

export interface EnabledModelsContextValue {
  models: EnabledModelInfo[];
  loading: boolean;
  refresh: () => Promise<void>;
}

export const EnabledModelsContext = createContext<EnabledModelsContextValue>({
  models: [],
  loading: false,
  refresh: async () => {},
});

export function EnabledModelsProvider({
  children,
}: {
  children?: React.ReactNode;
}): React.ReactElement {
  const [models, setModels] = useState<EnabledModelInfo[]>([]);
  const [loading, setLoading] = useState<boolean>(true);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/ai/models");
      if (!res.ok) {
        throw new Error(`Failed to load enabled models: HTTP ${res.status}`);
      }
      const data = await res.json();
      setModels(Array.isArray(data.models) ? data.models : []);
    } catch {
      // Keep the last successfully loaded list; it starts empty.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <EnabledModelsContext.Provider value={{ models, loading, refresh }}>
      {children}
    </EnabledModelsContext.Provider>
  );
}

export function useEnabledModels(): EnabledModelsContextValue {
  return useContext(EnabledModelsContext);
}
