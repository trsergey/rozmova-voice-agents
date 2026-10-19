import { BUDGET_BYTES, checkBundle, type ChunkInfo } from "./bundle-check";

const chunk = (fileName: string, over: Partial<ChunkInfo> = {}): ChunkInfo => ({
  fileName,
  isEntry: false,
  facadeModuleId: null,
  imports: [],
  moduleIds: [],
  size: 1000,
  ...over,
});

const entry = (over: Partial<ChunkInfo> = {}) =>
  chunk("index.js", {
    isEntry: true,
    moduleIds: ["/app/src/main.tsx", "/app/src/admin/guards.tsx", "/app/src/admin/session.ts"],
    ...over,
  });
const demo = (over: Partial<ChunkInfo> = {}) =>
  chunk("DemoPage.js", {
    facadeModuleId: "/app/src/demo/DemoPage.tsx",
    imports: ["index.js"],
    moduleIds: ["/app/src/demo/DemoPage.tsx"],
    ...over,
  });
const admin = chunk("AdminLayout.js", {
  facadeModuleId: "/app/src/admin/AdminLayout.tsx",
  imports: ["radix.js"],
  moduleIds: ["/app/src/admin/AdminLayout.tsx"],
});
const radix = chunk("radix.js", { moduleIds: ["/app/node_modules/radix-ui/dist/index.mjs"] });

test("a clean split passes", () => {
  expect(checkBundle([entry(), demo(), admin, radix])).toEqual([]);
});

test("admin code imported statically by the demo fails", () => {
  const problems = checkBundle([entry(), demo({ imports: ["index.js", "AdminLayout.js"] }), admin, radix]);

  expect(problems).toEqual(["demo loads admin-only code: src/admin/AdminLayout.tsx, radix-ui"]);
});

test("admin code in the entry fails", () => {
  const problems = checkBundle([entry({ moduleIds: ["/app/src/admin/bots/BotsPage.tsx"] }), demo()]);

  // The demo loads the entry, so it carries the leak too.
  expect(problems).toEqual([
    "entry loads admin-only code: src/admin/bots/BotsPage.tsx",
    "demo loads admin-only code: src/admin/bots/BotsPage.tsx",
  ]);
});

test("a demo merged into the entry fails", () => {
  expect(checkBundle([entry()])).toEqual(["the demo page is not a chunk of its own"]);
});

test("chunks over budget fail", () => {
  const problems = checkBundle([entry({ size: BUDGET_BYTES.entry + 1 }), demo({ size: BUDGET_BYTES.chunk + 1 })]);

  expect(problems).toEqual([
    expect.stringMatching(/^index\.js is \d+ bytes/),
    expect.stringMatching(/^DemoPage\.js is \d+ bytes/),
  ]);
});
