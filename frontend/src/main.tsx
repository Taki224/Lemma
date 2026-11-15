import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";

interface ErrorBoundaryProps {
  children: React.ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error("Workspace error caught by ErrorBoundary:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: "40px", color: "#f87171", fontFamily: "system-ui, sans-serif", maxWidth: "800px", margin: "40px auto", background: "#161922", border: "1px solid #2b3145", borderRadius: "10px" }}>
          <h2 style={{ color: "#f87171", marginTop: 0 }}>Workspace Encountered an Error</h2>
          <p style={{ color: "#9ca3af" }}>
            An unexpected error occurred during rendering:
          </p>
          <pre style={{ background: "#0f1117", padding: "16px", borderRadius: "6px", color: "#fca5a5", overflow: "auto", fontSize: "13px" }}>
            {this.state.error?.stack || this.state.error?.message || String(this.state.error)}
          </pre>
          <button
            type="button"
            onClick={() => window.location.reload()}
            style={{ marginTop: "16px", padding: "10px 20px", background: "#3b82f6", color: "white", border: "none", borderRadius: "6px", cursor: "pointer", fontWeight: 500 }}
          >
            Reload Page
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const rootElement = document.getElementById("root");
if (rootElement) {
  ReactDOM.createRoot(rootElement).render(
    <React.StrictMode>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </React.StrictMode>
  );
}
