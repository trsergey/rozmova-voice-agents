// A build-time guard for the code split (see src/router.tsx): the entry and the demo chat
// must not pull admin pages or Radix, and no chunk may outgrow its budget. It runs inside
// `vite build`, so CI fails the moment an import undoes the split.
import type { Plugin } from "vite";

export interface ChunkInfo {
  fileName: string;
  isEntry: boolean;
  facadeModuleId: string | null;
  imports: string[]; // static imports: loaded together with the chunk
  moduleIds: string[];
  size: number; // bytes
}

// The flow editor carries React Flow (~150 kB), so it gets its own, larger budget.
export const BUDGET_BYTES = { entry: 400_000, chunk: 100_000, editor: 250_000 };
const EDITOR = /\/src\/admin\/editor\/EditorPage\.tsx$/;
// The admin code every route needs: the session and its guards.
const SHARED_ADMIN = /\/src\/admin\/(guards|session)\.tsx?$/;
const ADMIN = /\/src\/admin\//;
const RADIX = /node_modules\/(radix-ui|@radix-ui)\//;

// Everything the browser loads with `start`: the chunk and its static imports, transitively.
function closure(start: ChunkInfo, byFile: Map<string, ChunkInfo>): ChunkInfo[] {
  const seen = new Map<string, ChunkInfo>();
  const visit = (chunk: ChunkInfo) => {
    if (seen.has(chunk.fileName)) return;
    seen.set(chunk.fileName, chunk);
    chunk.imports.forEach((file) => {
      const next = byFile.get(file);
      if (next) visit(next);
    });
  };
  visit(start);
  return [...seen.values()];
}

// "src/admin/AdminLayout.tsx", or the package name for a dependency.
function shortName(id: string): string {
  const pkg = /node_modules\/((?:@[^/]+\/)?[^/]+)/.exec(id);
  return pkg?.[1] ?? id.replace(/^.*?\/src\//, "src/");
}

function forbidden(chunks: ChunkInfo[]): string[] {
  const ids = chunks
    .flatMap((chunk) => chunk.moduleIds)
    .filter((id) => RADIX.test(id) || (ADMIN.test(id) && !SHARED_ADMIN.test(id)));
  return [...new Set(ids.map(shortName))];
}

// Problems with a bundle, or none.
export function checkBundle(chunks: ChunkInfo[]): string[] {
  const byFile = new Map(chunks.map((chunk) => [chunk.fileName, chunk]));
  const problems: string[] = [];
  const entry = chunks.find((chunk) => chunk.isEntry);
  const demo = chunks.find((chunk) => chunk.facadeModuleId?.endsWith("/src/demo/DemoPage.tsx"));
  if (!entry) problems.push("no entry chunk");
  if (!demo) problems.push("the demo page is not a chunk of its own");
  for (const [name, start] of [
    ["entry", entry],
    ["demo", demo],
  ] as const) {
    if (!start) continue;
    const leaked = forbidden(closure(start, byFile));
    if (leaked.length > 0) problems.push(`${name} loads admin-only code: ${leaked.join(", ")}`);
  }
  for (const chunk of chunks) {
    const budget = chunk.isEntry
      ? BUDGET_BYTES.entry
      : EDITOR.test(chunk.facadeModuleId ?? "")
        ? BUDGET_BYTES.editor
        : BUDGET_BYTES.chunk;
    if (chunk.size > budget) problems.push(`${chunk.fileName} is ${chunk.size} bytes, over its ${budget}`);
  }
  return problems;
}

export function bundleCheck(): Plugin {
  return {
    name: "rozmova-bundle-check",
    apply: "build",
    generateBundle(_, bundle) {
      const chunks: ChunkInfo[] = Object.values(bundle).flatMap((output) =>
        output.type === "chunk"
          ? [
              {
                fileName: output.fileName,
                isEntry: output.isEntry,
                facadeModuleId: output.facadeModuleId,
                imports: output.imports,
                moduleIds: output.moduleIds,
                size: new TextEncoder().encode(output.code).length,
              },
            ]
          : [],
      );
      const problems = checkBundle(chunks);
      if (problems.length > 0) this.error(`bundle check failed:\n- ${problems.join("\n- ")}`);
    },
  };
}
