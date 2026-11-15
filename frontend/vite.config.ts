import fs from "node:fs";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const runtime = "/tmp/lemma-frontend-runtime/node_modules";
const useStubBypass = fs.existsSync(`${runtime}/react/jsx-runtime.js`);
const pdfWorker = `${runtime}/pdfjs-dist/build/pdf.worker.min.mjs`;

function pdfWorkerDevFallback(): Plugin {
  return {
    name: "pdf-worker-dev-fallback",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url?.includes("pdf.worker.min.mjs") || !fs.existsSync(pdfWorker)) {
          next();
          return;
        }
        res.setHeader("Content-Type", "text/javascript");
        fs.createReadStream(pdfWorker).pipe(res);
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), ...(useStubBypass ? [pdfWorkerDevFallback()] : [])],
  resolve: useStubBypass
    ? {
        alias: [
          { find: /^react\/jsx-dev-runtime$/, replacement: `${runtime}/react/jsx-dev-runtime.js` },
          { find: /^react\/jsx-runtime$/, replacement: `${runtime}/react/jsx-runtime.js` },
          { find: /^react-dom\/client$/, replacement: `${runtime}/react-dom/client.js` },
          { find: /^react-dom$/, replacement: `${runtime}/react-dom` },
          { find: /^react$/, replacement: `${runtime}/react` },
          { find: /^@codemirror\/state$/, replacement: `${runtime}/@codemirror/state` },
          { find: /^@codemirror\/view$/, replacement: `${runtime}/@codemirror/view` },
          { find: /^codemirror-lang-latex$/, replacement: `${runtime}/codemirror-lang-latex` },
          { find: /^pdfjs-dist$/, replacement: `${runtime}/pdfjs-dist` },
        ],
      }
    : undefined,
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
});
