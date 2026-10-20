import { wordDiff } from "./wordDiff";

test("a changed phrase shows as removed and added words around the unchanged text", () => {
  expect(wordDiff("Вітаю! Чим можу допомогти?", "Привіт! Чим можу допомогти?")).toEqual([
    { kind: "removed", text: "Вітаю!" },
    { kind: "added", text: "Привіт!" },
    { kind: "same", text: " Чим можу допомогти?" },
  ]);
});

test("an inserted sentence is the only addition", () => {
  const pieces = wordDiff("Відповідай коротко.", "Відповідай коротко. Не вигадуй цін.");

  expect(pieces?.filter((p) => p.kind !== "same").map((p) => [p.kind, p.text.trim()])).toEqual([
    ["added", "Не вигадуй цін."],
  ]);
});

test("a long unchanged run collapses to its ends", () => {
  const words = Array.from({ length: 60 }, (_, n) => `w${n}`);
  const before = `${words.join(" ")} старе`;
  const after = `${words.join(" ")} нове`;

  const [same] = wordDiff(before, after) ?? [];

  expect(same?.kind).toBe("same");
  expect(same?.text.startsWith("w0 w1")).toBe(true);
  expect(same?.text).toContain(" … ");
  expect(same?.text).not.toContain("w30");
  expect(same?.text.trimEnd().endsWith("w59")).toBe(true);
});

test("texts too long to compare give null", () => {
  const huge = Array.from({ length: 2_500 }, (_, n) => `w${n}`).join(" ");

  expect(wordDiff(huge, `${huge} x`)).toBeNull();
});
