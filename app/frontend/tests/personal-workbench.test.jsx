// @vitest-environment jsdom
import { afterEach, beforeEach, it, expect, vi } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryReader } from "../src/pages/MemoryReader.jsx";
import { PersonalPage } from "../src/pages/PersonalPage.jsx";
import { setLang } from "../src/lib/i18n.jsx";

const data = vi.hoisted(() => ({ reader: null, profile: null }));
vi.mock("../src/lib/useResource.js", async (base) => ({
  ...(await base()),
  useResource(path) {
    return {
      data: path?.startsWith("/api/workbench/memories")
        ? data.reader
        : data.profile,
      error: null,
      loading: false,
      reload: vi.fn(),
    };
  },
}));
let root, container;
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  setLang("zh");
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("presents the current decision once and keeps the original sentence in one evidence block", async () => {
  const claim = "最终决定周报改用财务周统计";
  data.reader = {
    total: 1,
    items: [
      {
        id: "new",
        project_id: "p",
        category: "decision",
        heading: "周报按财务周统计",
        content: claim,
        sources: [{ id: "c", title: "口径讨论", quote: claim }],
        previous: [{ id: "old", title: "周报按自然周统计" }],
      },
    ],
  };
  const open = vi.fn();
  await act(() => root.render(<MemoryReader projectId="p" onOpen={open} />));
  const details = container.querySelector("details");
  expect(details.open).toBe(false);
  expect(details.textContent.split(claim)).toHaveLength(2);
  await act(() => details.querySelector("summary").click());
  expect(details.open).toBe(true);
  await act(() =>
    [...details.querySelectorAll("button")]
      .find((b) => b.textContent.includes("此前采用"))
      .click(),
  );
  expect(open).toHaveBeenCalledWith("old");
});

it("exports only the explicitly authored private method fields in a Skill draft", async () => {
  data.profile = {
    revision: 1,
    name: "Studio",
    bio: "Profile",
    assets: [
      {
        id: "a",
        title: "Table QA",
        inputs: "Two clean inputs",
        steps: "Compare totals",
        outputs: "Variance list",
        verification: "Ran a second sample",
        notes: "private unrelated note",
        excerpt: "Public excerpt",
        source_id: "",
        selected: false,
      },
    ],
  };
  let file;
  vi.stubGlobal(
    "URL",
    Object.assign(class extends URL {}, {
      createObjectURL(blob) {
        file = blob;
        return "blob:fixture";
      },
      revokeObjectURL() {},
    }),
  );
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  try {
    await act(() =>
      root.render(
        <PersonalPage
          route={{ section: "assets" }}
          navigate={vi.fn()}
          projects={[]}
          revision={0}
          onChanged={vi.fn()}
          draftStore={{ current: null }}
        />,
      ),
    );
    const button = [...container.querySelectorAll("button")].find((b) =>
      b.textContent.includes("导出私有 Skill"),
    );
    expect(button.disabled).toBe(false);
    await act(() => button.click());
    const content = await file.text();
    expect(content).toContain("name: method-a");
    expect(content).toContain("Compare totals");
    expect(content).not.toContain("private unrelated note");
    expect(content).not.toContain("Public excerpt");
  } finally {
    vi.unstubAllGlobals();
  }
});
