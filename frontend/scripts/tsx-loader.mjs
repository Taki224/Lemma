import fs from "node:fs";
import path from "node:path";
import { registerHooks, stripTypeScriptTypes } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { transformTsxToJs } from "./tsx-hooks.mjs";

registerHooks({
  resolve(specifier, context, nextResolve) {
    try {
      return nextResolve(specifier, context);
    } catch (err) {
      if (specifier.startsWith(".") && context.parentURL) {
        const parentDir = path.dirname(fileURLToPath(context.parentURL));
        for (const ext of [".tsx", ".ts", ".js", ".mjs", "/index.tsx", "/index.ts", "/index.js"]) {
          const candidate = path.resolve(parentDir, specifier + ext);
          if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
            return {
              url: pathToFileURL(candidate).href,
              shortCircuit: true,
            };
          }
        }
      }
      throw err;
    }
  },
  load(url, context, nextLoad) {
    if (url.endsWith(".tsx") || url.endsWith(".ts")) {
      const filePath = fileURLToPath(url);
      const rawSource = fs.readFileSync(filePath, "utf8");
      const transformed = transformTsxToJs(rawSource, filePath, stripTypeScriptTypes);
      return {
        format: "module",
        source: transformed,
        shortCircuit: true,
      };
    }
    return nextLoad(url, context);
  },
});
