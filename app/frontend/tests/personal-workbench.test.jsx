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

it("opens the requested card in preview and includes only selected public fields during editing", async () => {
  data.profile = {
    revision: 3,
    name: "Alpha studio",
    bio: "A local studio",
    assets: [
      {
        id: "one",
        title: "Study one",
        excerpt: "First outcome",
        selected: true,
        notes: "private source text",
      },
      {
        id: "two",
        title: "Study two",
        excerpt: "Second outcome",
        selected: false,
        notes: "sensitive note",
      },
    ],
  };
  const navigate = vi.fn();
  await act(() =>
    root.render(
      <PersonalPage
        route={{}}
        initialTab="profile"
        projects={[]}
        revision={0}
        onChanged={vi.fn()}
        navigate={navigate}
        draftStore={{ current: null }}
      />,
    ),
  );
  const card = () => container.querySelector("article.profile-preview");
  expect(card().textContent).toContain("Study one");
  expect(card().textContent).not.toContain("Study two");
  expect(container.querySelector("textarea")).toBeNull();
  const draftBrief = () =>
    [...container.querySelectorAll("button")].find(
      (button) => button.textContent === "起草协作说明",
    );
  await act(() => draftBrief().click());
  expect(navigate).toHaveBeenCalledWith({ page: "exchange" });
  const edit = [...container.querySelectorAll("button")].find(
    (button) => button.textContent === "编辑名片",
  );
  await act(() => edit.click());
  const includeSecond = container.querySelectorAll('input[type="checkbox"]')[1];
  await act(() => includeSecond.click());
  expect(draftBrief()).toBeUndefined();
  expect(card().textContent).toContain("Second outcome");
  expect(card().textContent).not.toContain("sensitive note");
  expect(container.querySelector('[data-export-kind="profile"]').disabled).toBe(
    true,
  );
});

it("selects the requested project for evidence and puts an adopted suggestion directly into its editor", async () => {
  const api = await import("../src/lib/api.js");
  const read = vi.spyOn(api, "mutate").mockResolvedValue({
    items: [
      {
        topic: "charts",
        projects: 1,
        samples: 1,
        evidence: [
          {
            id: "ev1",
            project_id: "proj1",
            title: "Design charts",
            version: 4,
          },
        ],
      },
    ],
    more: false,
  });
  data.profile = {
    revision: 2,
    name: "",
    bio: "",
    assets: [{ id: "other", title: "Older work", excerpt: "", notes: "" }],
  };
  const store = { current: null };
  const navigate = vi.fn();
  await act(() =>
    root.render(
      <PersonalPage
        route={{ section: "assets", project: "proj1" }}
        projects={[{ id: "proj1", name: "Studio" }]}
        revision={0}
        onChanged={vi.fn()}
        navigate={navigate}
        draftStore={store}
      />,
    ),
  );
  expect(read).toHaveBeenCalledWith("/api/workbench/candidates", "POST", {
    project_ids: ["proj1"],
    offset: 0,
  });
  const details = container.querySelector("details.studio-suggestions");
  expect(details.open).toBe(true);
  await act(() =>
    [...details.querySelectorAll("button")]
      .find((button) => button.textContent === "采用为能力草稿")
      .click(),
  );
  expect(container.querySelector(".studio-asset-editor input").value).toBe(
    "Design charts",
  );
  expect(store.current.assets[1].evidence[0].version).toBe(4);
  await act(() =>
    [...container.querySelectorAll(".studio-asset-editor button")]
      .find((button) => button.textContent === "Design charts")
      .click(),
  );
  expect(navigate).toHaveBeenCalledWith({ section: "assets", record: "ev1" });
});

it.each([
  { assets: [] },
  {
    assets: [
      {
        id: "private",
        title: "Personal work",
        notes: "Private",
        selected: false,
      },
    ],
  },
])(
  "keeps an empty or unselected portfolio out of collaboration",
  async ({ assets }) => {
    data.profile = { revision: 2, name: "Private studio", bio: "", assets };
    await act(() =>
      root.render(
        <PersonalPage
          route={{ section: "profile" }}
          projects={[]}
          revision={0}
          onChanged={vi.fn()}
          navigate={vi.fn()}
          draftStore={{ current: null }}
        />,
      ),
    );
    expect(container.textContent).not.toContain("起草协作说明");
  },
);
