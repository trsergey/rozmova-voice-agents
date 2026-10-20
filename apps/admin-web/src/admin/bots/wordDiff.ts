// A word-level diff of two texts for the version diff (FR-12): a prompt edit of one phrase
// shows as that phrase, not as two near-identical walls of text. Plain LCS over words and the
// whitespace between them; long unchanged runs collapse to their ends around "…".

export interface Piece {
  kind: "same" | "removed" | "added";
  text: string;
}

// Beyond this many token pairs the table gets large; the caller shows before and after.
const MAX_CELLS = 4_000_000;
// An unchanged run longer than this many words keeps this many on each side of "…".
const COLLAPSE_OVER = 24;
const KEEP = 8;

export function wordDiff(before: string, after: string): Piece[] | null {
  const a = before.split(/(\s+)/).filter(Boolean);
  const b = after.split(/(\s+)/).filter(Boolean);
  if (a.length * b.length > MAX_CELLS) return null;

  // lcs(i, j): length of the longest common subsequence of a[i:] and b[j:], row-major.
  const width = b.length + 1;
  const table = new Uint32Array((a.length + 1) * width);
  const lcs = (i: number, j: number) => table[i * width + j] ?? 0;
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      table[i * width + j] = a[i] === b[j] ? lcs(i + 1, j + 1) + 1 : Math.max(lcs(i + 1, j), lcs(i, j + 1));
    }
  }

  const pieces: Piece[] = [];
  const push = (kind: Piece["kind"], text = "") => {
    const last = pieces.at(-1);
    if (last?.kind === kind) last.text += text;
    else pieces.push({ kind, text });
  };
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      push("same", a[i]);
      i++;
      j++;
    } else if (lcs(i + 1, j) >= lcs(i, j + 1)) {
      push("removed", a[i++]);
    } else {
      push("added", b[j++]);
    }
  }
  while (i < a.length) push("removed", a[i++]);
  while (j < b.length) push("added", b[j++]);
  return pieces.map((piece) => (piece.kind === "same" ? { ...piece, text: collapse(piece.text) } : piece));
}

function collapse(text: string): string {
  const words = text.split(/(\s+)/);
  // Words sit at even indexes, the whitespace between them at odd ones.
  const count = Math.ceil(words.length / 2);
  if (count <= COLLAPSE_OVER) return text;
  const head = words.slice(0, KEEP * 2).join("");
  const tail = words.slice(-KEEP * 2).join("");
  return `${head} … ${tail}`;
}
