// The `[n]` markers a knowledge-base answer carries after each statement (SDD "RAG"). A
// subscriber reads the reply without them and the sources as a line of titles; the admin
// keeps them as small superscripts, which is where a reply's facts are checked.
import type { ReactNode } from "react";

import type { Source } from "../../../../packages/contracts/ts/protocol.server";

// "[1]", "[2][3]" or "[1, 2]", with the space before them.
const MARKERS = /\s*\[\d+(?:\s*,\s*\d+)*\]/g;
// A marker still being streamed at the end of the text: "[", "[1", "[1, ".
const PARTIAL = /\s*\[[\d,\s]*$/;

export function withoutMarkers(text: string, streaming = false): string {
  const clean = text.replace(MARKERS, "");
  return streaming ? clean.replace(PARTIAL, "") : clean;
}

// Document titles in order of first citation, each once.
export function sourceTitles(sources: Source[]): string[] {
  return [...new Set(sources.map((source) => source.document_title))];
}

// The text with its markers as superscripts; a marker's title is its source when known.
export function withSuperscripts(text: string, sources: Source[] = []): ReactNode[] {
  const titles = new Map(sources.map((s) => [s.marker, s.section ? `${s.document_title} — ${s.section}` : s.document_title]));
  const parts: ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(/\[(\d+(?:\s*,\s*\d+)*)\]/g)) {
    // The space before a marker goes with it: «грн¹», not «грн ¹».
    parts.push(text.slice(last, match.index).replace(/\s+$/, ""));
    const numbers = (match[1] ?? "").split(",").map((n) => Number(n.trim()));
    for (const n of numbers) {
      parts.push(
        <sup key={`${match.index}-${n}`} className="ml-px text-[0.85em] font-medium text-accent" title={titles.get(n)}>
          {n}
        </sup>,
      );
    }
    last = match.index + match[0].length;
  }
  parts.push(text.slice(last));
  return parts;
}
