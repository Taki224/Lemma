import { useEffect, useMemo, useState } from "react";
import { parseBibFile, type BibEntry } from "../utils/bibParser";

interface ProjectFileLike {
  path: string;
  is_dir?: boolean;
}

interface UseBibEntriesOptions {
  projectId: string | null | undefined;
  files: ProjectFileLike[];
  activeFile: string;
  fileContent: string;
  fileContentPath: string | null;
  dirtyBuffers: Record<string, string>;
  refreshKey?: number;
}

/**
 * Load and parse all `.bib` files in the active project, reflecting unsaved edits immediately.
 */
export function useBibEntries({
  projectId,
  files,
  activeFile,
  fileContent,
  fileContentPath,
  dirtyBuffers,
  refreshKey = 0,
}: UseBibEntriesOptions): BibEntry[] {
  const bibPaths = useMemo(
    () =>
      files
        .filter((f) => !f.is_dir && f.path.toLowerCase().endsWith(".bib"))
        .map((f) => f.path)
        .sort(),
    [files]
  );
  const bibPathsKey = bibPaths.join("\0");

  const [fetchedBibs, setFetchedBibs] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!projectId || bibPaths.length === 0) {
      setFetchedBibs({});
      return;
    }

    let cancelled = false;
    Promise.all(
      bibPaths.map(async (path) => {
        try {
          const res = await fetch(`/api/projects/${projectId}/files/${path}`);
          if (!res.ok) return [path, ""] as const;
          const data = (await res.json()) as { content?: string };
          return [path, typeof data.content === "string" ? data.content : ""] as const;
        } catch {
          return [path, ""] as const;
        }
      })
    ).then((pairs) => {
      if (cancelled) return;
      const next: Record<string, string> = {};
      for (const [path, text] of pairs) {
        if (text) next[path] = text;
      }
      setFetchedBibs(next);
    });

    return () => {
      cancelled = true;
    };
  }, [projectId, bibPathsKey, refreshKey]);

  return useMemo(() => {
    if (bibPaths.length === 0) return [];
    const seen = new Set<string>();
    const combined: BibEntry[] = [];

    for (const path of bibPaths) {
      const text =
        dirtyBuffers[path] !== undefined
          ? dirtyBuffers[path]
          : path === activeFile && fileContentPath === path
            ? fileContent
            : fetchedBibs[path] ?? "";
      if (!text) continue;
      for (const entry of parseBibFile(text)) {
        if (!seen.has(entry.key)) {
          seen.add(entry.key);
          combined.push(entry);
        }
      }
    }

    return combined;
  }, [bibPaths, dirtyBuffers, activeFile, fileContentPath, fileContent, fetchedBibs]);
}
