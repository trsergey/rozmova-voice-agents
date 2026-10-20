import { render, screen } from "@testing-library/react";

import { sourceTitles, withoutMarkers, withSuperscripts } from "./citations";

const source = (marker: number, document_title: string, section: string | null = null) => ({
  marker,
  chunk_id: `c${marker}`,
  document_id: document_title,
  document_title,
  section,
});

test.each([
  ["Безлім коштує 395 грн [1].", "Безлім коштує 395 грн."],
  ["Зони роумінгу [2][3], інтернет [1, 2].", "Зони роумінгу, інтернет."],
  ["Без джерел.", "Без джерел."],
])("markers are dropped from %j", (text, clean) => {
  expect(withoutMarkers(text)).toBe(clean);
});

test("a marker cut by the stream is hidden only while streaming", () => {
  expect(withoutMarkers("395 грн [", true)).toBe("395 грн");
  expect(withoutMarkers("395 грн [1, ", true)).toBe("395 грн");
  expect(withoutMarkers("масив [", false)).toBe("масив [");
});

test("source titles come once each, in citation order", () => {
  expect(sourceTitles([source(1, "Тариф «Безлім»"), source(2, "Роумінг"), source(3, "Тариф «Безлім»")])).toEqual([
    "Тариф «Безлім»",
    "Роумінг",
  ]);
});

test("superscripts carry the source they cite", () => {
  render(<p>{withSuperscripts("395 грн [1][2].", [source(1, "Тариф «Безлім»", "Ціна")])}</p>);

  expect(screen.getByText("395 грн", { exact: false })).toHaveTextContent("395 грн12.");
  expect(screen.getByText("1", { selector: "sup" })).toHaveAttribute("title", "Тариф «Безлім» — Ціна");
  expect(screen.getByText("2", { selector: "sup" })).not.toHaveAttribute("title");
});
