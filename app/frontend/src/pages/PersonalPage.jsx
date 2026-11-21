import { useEffect, useState } from "react";
import {
  Button,
  ErrorNotice,
  PageHeader,
  ResourceState,
  SegmentedTabs,
} from "../components/ui.jsx";
import { mutate, request } from "../lib/api.js";
import { t } from "../lib/i18n.jsx";
import { queryString, useResource } from "../lib/useResource.js";
import { CapabilitySuggestions } from "../components/CapabilitySuggestions.jsx";
import { MemoryReader } from "./MemoryReader.jsx";
import { ContentDetail } from "./KnowledgePage.jsx";

const tabs = [
  ["memory", "个人记忆"],
  ["assets", "能力与作品"],
  ["profile", "简介草稿"],
];
const emptyAsset = () => ({
  id: crypto.randomUUID(),
  title: "",
  notes: "",
  excerpt: "",
  selected: false,
  source_id: "",
  source_version: 0,
});

function EvidencePicker({ projects, onPick, revision }) {
  const [project, setProject] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const source = useResource(
    project
      ? "/api/content/index" +
          queryString({
            project_id: project,
            query,
            status: "active",
            offset,
            limit: 12,
          })
      : null,
    revision,
  );
  return (
    <details className="asset-picker">
      <summary>{t("从项目选取依据")}</summary>
      <label>
        {t("选择项目")}
        <select
          value={project}
          onChange={(e) => {
            setProject(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">{t("选择项目")}</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      {project && (
        <>
          <label>
            {t("搜索标题或正文")}
            <input
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setOffset(0);
              }}
            />
          </label>
          <ResourceState resource={source}>
            {source.data?.items?.map((item) => (
              <Button key={item.id} onClick={() => onPick(item)}>
                {item.title}
              </Button>
            ))}
            {!source.data?.items?.length && <p>{t("没有匹配的资料")}</p>}
            {offset > 0 && (
              <Button onClick={() => setOffset(offset - 12)}>
                {t("上一页")}
              </Button>
            )}
            {offset + 12 < source.data?.total && (
              <Button onClick={() => setOffset(offset + 12)}>
                {t("下一页")}
              </Button>
            )}
          </ResourceState>
        </>
      )}
    </details>
  );
}

function Preview({ profile }) {
  return (
    <article className="profile-preview">
      <h2>{profile.name || t("我的工作简介")}</h2>
      {profile.bio && <p>{profile.bio}</p>}
      {profile.assets
        .filter((a) => a.selected)
        .map((a) => (
          <section key={a.id}>
            <h3>{a.title}</h3>
            <p>{a.excerpt}</p>
          </section>
        ))}
    </article>
  );
}

function downloadText(filename, body) {
  const url = URL.createObjectURL(
    new Blob([body], { type: "text/plain;charset=utf-8" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function ProfileEditor({ section, projects, revision, draftStore }) {
  const result = useResource("/api/workbench/profile", revision);
  const [draft, setDraft] = useState(draftStore.current);
  const [saved, setSaved] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const base =
    (saved?.revision || 0) > (result.data?.revision || 0) ? saved : result.data;
  const profile = draft || base;
  useEffect(() => {
    if (!draft) return;
    const warn = (event) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [draft]);
  function change(next) {
    draftStore.current = next;
    setDraft(next);
    setNotice("");
  }
  function changeAsset(id, patch) {
    change({
      ...profile,
      assets: profile.assets.map((a) => (a.id === id ? { ...a, ...patch } : a)),
    });
  }
  async function save() {
    setBusy(true);
    setError(null);
    try {
      const next = await mutate("/api/workbench/profile", "PUT", profile);
      setSaved(next);
      change(null);
      result.reload();
      setNotice(t("已保存到本机"));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  async function download() {
    setBusy(true);
    setError(null);
    try {
      const snapshot = await request("/api/workbench/profile/export");
      if (snapshot.revision !== profile.revision) {
        result.reload();
        setNotice(t("资料已更新，请重新预览后导出"));
        return;
      }
      const lines = [
        snapshot.name,
        snapshot.bio,
        ...snapshot.assets.map((a) =>
          [a.title, a.excerpt].filter(Boolean).join("\n"),
        ),
      ];
      downloadText(
        "Senrelay-profile-v" + snapshot.revision + ".txt",
        lines.filter(Boolean).join("\n\n"),
      );
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  return (
    <ResourceState resource={result}>
      {profile && (
        <fieldset disabled={busy} className="personal-form">
          {section === "assets" ? (
            <>
              <div className="reader-heading">
                <div>
                  <h2>{t("能力与作品")}</h2>
                  <p>{t("记下你能交付什么，关联项目里的作品或经验。")}</p>
                </div>
                <Button
                  onClick={() =>
                    change({
                      ...profile,
                      assets: [...profile.assets, emptyAsset()],
                    })
                  }
                >
                  {t("添加能力或作品")}
                </Button>
              </div>
              <CapabilitySuggestions
                projects={projects}
                profile={profile}
                change={change}
                makeAsset={emptyAsset}
              />
              {!profile.assets.length && (
                <p>{t("从一个完成的项目开始，记录你的贡献和交付结果。")}</p>
              )}
              {profile.assets.map((a, i) => (
                <section className="personal-asset" key={a.id}>
                  <label>
                    {t("能力或作品名称")}
                    <input
                      maxLength={200}
                      value={a.title}
                      onChange={(e) =>
                        changeAsset(a.id, { title: e.target.value })
                      }
                    />
                  </label>
                  <label>
                    {t("可展示的说明")}
                    <textarea
                      maxLength={4000}
                      value={a.excerpt}
                      onChange={(e) =>
                        changeAsset(a.id, { excerpt: e.target.value })
                      }
                    />
                  </label>
                  <label>
                    {t("私人笔记")}
                    <textarea
                      maxLength={8000}
                      value={a.notes}
                      onChange={(e) =>
                        changeAsset(a.id, { notes: e.target.value })
                      }
                    />
                  </label>
                  <details className="asset-picker">
                    <summary>{t("可复用的方法")}</summary>
                    <label>
                      {t("需要的输入")}
                      <textarea
                        maxLength={2000}
                        value={a.inputs || ""}
                        onChange={(e) =>
                          changeAsset(a.id, { inputs: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      {t("执行步骤")}
                      <textarea
                        maxLength={8000}
                        value={a.steps || ""}
                        onChange={(e) =>
                          changeAsset(a.id, { steps: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      {t("预期交付")}
                      <textarea
                        maxLength={2000}
                        value={a.outputs || ""}
                        onChange={(e) =>
                          changeAsset(a.id, { outputs: e.target.value })
                        }
                      />
                    </label>
                    <label>
                      {t("新样例的验证记录")}
                      <textarea
                        maxLength={2000}
                        value={a.verification || ""}
                        onChange={(e) =>
                          changeAsset(a.id, { verification: e.target.value })
                        }
                      />
                    </label>
                    <Button
                      disabled={
                        !!draft ||
                        !a.inputs?.trim() ||
                        !a.steps?.trim() ||
                        !a.outputs?.trim()
                      }
                      onClick={() => {
                        const name =
                          "method-" +
                          a.id
                            .toLowerCase()
                            .replace(/[^a-z0-9-]/g, "")
                            .slice(0, 48);
                        const sections = [
                          "---",
                          "name: " + name,
                          "description: " + JSON.stringify(a.title),
                          "---",
                          "# " + a.title,
                          "## " + t("需要的输入"),
                          a.inputs,
                          "## " + t("执行步骤"),
                          a.steps,
                          "## " + t("预期交付"),
                          a.outputs,
                        ];
                        if (a.verification)
                          sections.push(
                            "## " + t("新样例的验证记录"),
                            a.verification,
                          );
                        downloadText("SKILL.md", sections.join("\n"));
                      }}
                    >
                      {t("导出私有 Skill 草稿")}
                    </Button>
                  </details>
                  {a.source_id && (
                    <p>
                      {t("关联依据")} · {a.source_title || t("来源已删除")}
                      {a.source_current_version !== a.source_version && (
                        <strong> · {t("请重新核对依据")}</strong>
                      )}
                    </p>
                  )}
                  <label>
                    {t("本人和 Agent 的分工")}
                    <textarea
                      maxLength={2000}
                      value={a.contribution || ""}
                      onChange={(e) =>
                        changeAsset(a.id, { contribution: e.target.value })
                      }
                    />
                  </label>
                  {!!a.evidence?.length && (
                    <ul>
                      {a.evidence.map((ref) => (
                        <li key={ref.id}>
                          {ref.title || t("来源已删除")} · {t("版本")}{" "}
                          {ref.version}
                          {ref.current_version !== ref.version && (
                            <strong> · {t("请重新核对依据")}</strong>
                          )}
                          <Button
                            onClick={() =>
                              changeAsset(a.id, {
                                evidence: a.evidence.filter(
                                  (r) => r.id !== ref.id,
                                ),
                              })
                            }
                          >
                            {t("解除关联")}
                          </Button>
                        </li>
                      ))}
                    </ul>
                  )}
                  <EvidencePicker
                    projects={projects}
                    revision={revision}
                    onPick={(item) =>
                      changeAsset(a.id, {
                        source_id: item.id,
                        source_version: item.version,
                        source_title: item.title,
                        source_current_version: item.version,
                        title: a.title || item.title,
                      })
                    }
                  />
                  <div className="toolbar-actions">
                    {a.source_id && (
                      <Button
                        onClick={() =>
                          changeAsset(a.id, {
                            source_id: "",
                            source_version: 0,
                          })
                        }
                      >
                        {t("解除关联")}
                      </Button>
                    )}
                    {i > 0 && (
                      <Button
                        onClick={() => {
                          const assets = [...profile.assets];
                          [assets[i - 1], assets[i]] = [
                            assets[i],
                            assets[i - 1],
                          ];
                          change({ ...profile, assets });
                        }}
                      >
                        {t("上移")}
                      </Button>
                    )}
                    <Button
                      onClick={() =>
                        change({
                          ...profile,
                          assets: profile.assets.filter((it) => it.id !== a.id),
                        })
                      }
                    >
                      {t("移除条目")}
                    </Button>
                  </div>
                </section>
              ))}
            </>
          ) : (
            <>
              <h2>{t("简介草稿")}</h2>
              <p>{t("选入简介的内容会出现在预览和导出的文本里。")}</p>
              <div className="profile-columns">
                <section>
                  <label>
                    {t("显示名称")}
                    <input
                      maxLength={160}
                      value={profile.name}
                      onChange={(e) =>
                        change({ ...profile, name: e.target.value })
                      }
                    />
                  </label>
                  <label>
                    {t("个人介绍")}
                    <textarea
                      maxLength={4000}
                      value={profile.bio}
                      onChange={(e) =>
                        change({ ...profile, bio: e.target.value })
                      }
                    />
                  </label>
                  <h3>{t("选入简介的作品")}</h3>
                  {profile.assets.map((a) => (
                    <label className="check-label" key={a.id}>
                      <input
                        type="checkbox"
                        checked={a.selected}
                        onChange={(e) =>
                          changeAsset(a.id, { selected: e.target.checked })
                        }
                      />
                      {a.title}
                    </label>
                  ))}
                  <Button
                    disabled={
                      !profile.assets.some((a) => a.selected && a.excerpt) ||
                      !!profile.bio.trim()
                    }
                    onClick={() =>
                      change({
                        ...profile,
                        bio: profile.assets
                          .filter((a) => a.selected && a.excerpt)
                          .map((a) => a.excerpt)
                          .join("\n")
                          .slice(0, 4000),
                      })
                    }
                  >
                    {t("用已选说明起草介绍")}
                  </Button>
                </section>
                <Preview profile={profile} />
              </div>
            </>
          )}
          <ErrorNotice error={error} />
          {error?.status === 409 && (
            <Button
              onClick={() => {
                change(null);
                setSaved(null);
                setError(null);
                result.reload();
              }}
            >
              {t("放弃本次编辑并读取最新版")}
            </Button>
          )}
          <div className="toolbar-actions">
            <Button
              variant="primary"
              busy={busy}
              disabled={!draft || profile.assets.some((a) => !a.title.trim())}
              onClick={save}
            >
              {t("保存到本机")}
            </Button>
            {draft && (
              <Button
                onClick={() => {
                  change(null);
                  setError(null);
                }}
              >
                {t("撤销未保存的编辑")}
              </Button>
            )}
            {section === "profile" && (
              <Button
                disabled={
                  !!draft ||
                  !(
                    profile.name ||
                    profile.bio ||
                    profile.assets.some((a) => a.selected)
                  )
                }
                onClick={download}
              >
                {t("导出当前简介")}
              </Button>
            )}
            <small role="status">{draft ? t("有未保存的编辑") : notice}</small>
          </div>
        </fieldset>
      )}
    </ResourceState>
  );
}

export function PersonalPage({
  route,
  navigate,
  projects,
  revision,
  onChanged,
  draftStore,
}) {
  const section = tabs.some(([key]) => key === route.section)
    ? route.section
    : "memory";
  const [adding, setAdding] = useState(false);
  const [claim, setClaim] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  async function add(event) {
    event.preventDefault();
    if (!claim.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await mutate("/api/content", "POST", {
        kind: "memory",
        category: "preference",
        scope: "global",
        title: claim.trim().slice(0, 100),
        content: claim.trim(),
        tags: ["personal"],
      });
      setClaim("");
      setAdding(false);
      onChanged();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <PageHeader
        title={t("我的")}
        description={t("管理跨项目的工作约定、作品和个人简介。")}
      />
      <SegmentedTabs label={t("个人工作台")}>
        {tabs.map(([id, label]) => (
          <button
            key={id}
            className={section === id ? "active" : ""}
            aria-current={section === id ? "page" : undefined}
            onClick={() => navigate({ section: id })}
          >
            {t(label)}
          </button>
        ))}
      </SegmentedTabs>
      {section === "memory" ? (
        route.record ? (
          <ContentDetail
            key={route.record}
            id={route.record}
            projects={projects}
            revision={revision}
            onChanged={onChanged}
            onBack={() => navigate({ record: null })}
            onProject={(project) => navigate({ page: "projects", project })}
          />
        ) : (
          <>
            {adding && (
              <form className="personal-form" onSubmit={add}>
                <label>
                  {t("新的个人约定")}
                  <textarea
                    required
                    autoFocus
                    maxLength={4000}
                    value={claim}
                    onChange={(e) => setClaim(e.target.value)}
                  />
                </label>
                <ErrorNotice error={error} />
                <div className="toolbar-actions">
                  <Button type="submit" busy={busy}>
                    {t("保存到本机")}
                  </Button>
                  <Button disabled={busy} onClick={() => setAdding(false)}>
                    {t("取消")}
                  </Button>
                </div>
              </form>
            )}
            <MemoryReader
              revision={revision}
              onOpen={(record) => navigate({ section: "memory", record })}
              onAdd={() => setAdding(true)}
            />
          </>
        )
      ) : (
        <ProfileEditor
          section={section}
          projects={projects}
          revision={revision}
          draftStore={draftStore}
        />
      )}
    </>
  );
}
