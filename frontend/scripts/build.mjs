import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { transformTsxToJs } from "./tsx-hooks.mjs";

const frontendRoot = path.resolve(import.meta.dirname, "..");
const distDir = path.join(frontendRoot, "dist");

// Check for vite entry script in node_modules
const viteScript = path.join(frontendRoot, "node_modules", "vite", "bin", "vite.js");
const hasVite = fs.existsSync(viteScript);

let viteSuccess = false;
if (hasVite) {
  const result = spawnSync(process.execPath, [viteScript, "build"], {
    cwd: frontendRoot,
    stdio: "inherit",
  });
  if (result.status === 0) {
    viteSuccess = true;
    console.log("Successfully built with Vite into dist/");
  }
}

if (!viteSuccess) {
  // Ensure clean dist/ directory
  fs.mkdirSync(path.join(distDir, "assets"), { recursive: true });

  // Fallback build using transformTsxToJs
  let indexHtml = fs.readFileSync(path.join(frontendRoot, "index.html"), "utf8");
  indexHtml = indexHtml
    .replace('/src/index.css', '/index.css')
    .replace('/src/main.tsx', '/assets/main.js');
  fs.writeFileSync(path.join(distDir, "index.html"), indexHtml, "utf8");

  if (fs.existsSync(path.join(frontendRoot, "src", "index.css"))) {
    fs.copyFileSync(
      path.join(frontendRoot, "src", "index.css"),
      path.join(distDir, "index.css")
    );
  }

  const srcDir = path.join(frontendRoot, "src");
  if (fs.existsSync(srcDir)) {
    const compileDir = (dir, relBase = "") => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        if (entry.name === "__tests__") continue;
        const full = path.join(dir, entry.name);
        const rel = path.join(relBase, entry.name);
        if (entry.isDirectory()) {
          compileDir(full, rel);
        } else if (/\.(tsx|ts)$/.test(entry.name)) {
          const jsCode = transformTsxToJs(fs.readFileSync(full, "utf8"), full);
          const outPath = path.join(distDir, "assets", rel.replace(/\.(tsx|ts)$/, ".js"));
          fs.mkdirSync(path.dirname(outPath), { recursive: true });
          fs.writeFileSync(outPath, jsCode, "utf8");
        }
      }
    };
    compileDir(srcDir);
  }
  console.log("Built fallback assets into dist/");
}
