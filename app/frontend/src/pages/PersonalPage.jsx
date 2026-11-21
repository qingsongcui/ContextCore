import { MethodLibraryActions } from '../components/MethodLibraryActions.jsx';
import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  BookOpen,
  BriefcaseBusiness,
  Check,
  ChevronRight,
  CircleUserRound,
  FileText,
  FolderOpen,
  GalleryVerticalEnd,
  IdCard,
  Link2,
  PencilLine,
  Plus,
} from "lucide-react";
import "../styles/personal-studio.css";
import {
  Button,
  ErrorNotice,
  PageHeader,
  ResourceState,
} from "../components/ui.jsx";
import { downloadText } from "../lib/downloadText.js";
import { mutate, request } from "../lib/api.js";
import { t } from "../lib/i18n.jsx";
import { queryString, useResource } from "../lib/useResource.js";
import { CapabilitySuggestions } from "../components/CapabilitySuggestions.jsx";
import { MemoryReader } from "./MemoryReader.jsx";
import { ContentDetail } from "./KnowledgePage.jsx";

const tabs = [
  ["memory", "个人记忆", BookOpen],
  ["assets", "能力与作品", BriefcaseBusiness],
  ["profile", "Agent 名片", IdCard],
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
  const selected = profile.assets.filter((asset) => asset.selected);
  const initial = Array.from(profile.name.trim())[0];
  return (
    <article className="studio-card profile-preview" aria-label={t("名片预览")}>
      <div className="studio-card-top">
        <span className="studio-mark" aria-hidden="true">
          {initial || <CircleUserRound size={25} />}
        </span>
        <span>
          <IdCard size={15} aria-hidden="true" />
          {t("Agent 名片")}
        </span>
      </div>
      <h2>{profile.name || t("我的工作简介")}</h2>
      {profile.bio ? (
        <p className="studio-bio">{profile.bio}</p>
      ) : (
        <p className="studio-placeholder">
          {t("写下你和 Agent 擅长完成的工作。")}
        </p>
      )}
      {selected.length > 0 && (
        <div className="studio-showcase">
          <h3>{t("选入简介的作品")}</h3>
          {selected.map((asset) => (
            <section key={asset.id}>
              <h4>{asset.title}</h4>
              {asset.excerpt && <p>{asset.excerpt}</p>}
            </section>
          ))}
        </div>
      )}
    </article>
  );
}

function AssetTextField({ label, asset, field, maxLength, onChange }) {
  return (
    <label>
      {label}
      <textarea
        maxLength={maxLength}
        value={asset[field] || ""}
        onChange={(event) =>
          onChange(asset.id, { [field]: event.target.value })
        }
      />
    </label>
  );
}

function ProfileEditor({
  section,
  projects,
  revision,
  draftStore,
  navigate,
  onChanged,
  sourceProject,
  selectedAssetId,
  setSelectedAssetId,
}) {
  const result = useResource("/api/workbench/profile", revision);
  const [draft, setDraft] = useState(draftStore.current);
  const [saved, setSaved] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const base =
    (saved?.revision || 0) > (result.data?.revision || 0) ? saved : result.data;
  const profile = draft || base;
  const [editing, setEditing] = useState(false);
  const unnamedAsset = draft?.assets.find((asset) => !asset.title.trim());
  const assetEditor = useRef(null);
  const focusNewAsset = useRef(false);
  const activeAsset =
    profile?.assets.find((asset) => asset.id === selectedAssetId) ||
    profile?.assets[0];
  useEffect(() => {
    if (focusNewAsset.current && assetEditor.current) {
      assetEditor.current.focus();
      focusNewAsset.current = false;
    }
  }, [activeAsset?.id, section]);
  function addAsset(asset) {
    focusNewAsset.current = true;
    setSelectedAssetId(asset.id);
    change({ ...profile, assets: [...profile.assets, asset] });
  }
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
      onChanged?.();
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
              <div className="studio-section-heading">
                <div>
                  <h2>{t("作品集")}</h2>
                  <p>{t("记下你能交付什么，关联项目里的作品或经验。")}</p>
                </div>
                <Button
                  variant="primary"
                  icon={Plus}
                  disabled={profile.assets.length >= 200}
                  onClick={() => addAsset(emptyAsset())}
                >
                  {t("添加能力或作品")}
                </Button>
              </div>
              <CapabilitySuggestions
                key={sourceProject || "all"}
                sourceProject={sourceProject}
                projects={projects}
                profile={profile}
                change={change}
                makeAsset={emptyAsset}
                onAdopt={addAsset}
                onProjects={() => navigate({ page: "projects" })}
              />
              {!profile.assets.length && (
                <div className="studio-blank">
                  <GalleryVerticalEnd size={26} aria-hidden="true" />
                  <p>{t("从一个完成的项目开始，记录你的贡献和交付结果。")}</p>
                </div>
              )}
              {activeAsset && (
                <div className="studio-library">
                  <nav className="studio-asset-list" aria-label={t("作品集")}>
                    {profile.assets.map((asset) => (
                      <button
                        key={asset.id}
                        type="button"
                        aria-current={
                          activeAsset.id === asset.id ? "true" : undefined
                        }
                        onClick={() => setSelectedAssetId(asset.id)}
                      >
                        <FileText size={16} aria-hidden="true" />
                        <span>
                          <strong>{asset.title || t("未命名作品")}</strong>
                          {asset.selected && <small>{t("已选入名片")}</small>}
                        </span>
                        <ChevronRight size={14} aria-hidden="true" />
                      </button>
                    ))}
                  </nav>

                  <section className="studio-asset-editor" key={activeAsset.id}>
                    <label>
                      {t("能力或作品名称")}
                      <input
                        ref={assetEditor}
                        maxLength={200}
                        value={activeAsset.title}
                        onChange={(e) =>
                          changeAsset(activeAsset.id, { title: e.target.value })
                        }
                      />
                    </label>
                    <AssetTextField
                      label={t("可展示的说明")}
                      field="excerpt"
                      asset={activeAsset}
                      maxLength={4000}
                      onChange={changeAsset}
                    />
                    <details className="studio-disclosure">
                      <summary>
                        <PencilLine size={16} aria-hidden="true" />
                        {t("贡献与私人笔记")}
                      </summary>
                      <AssetTextField
                        label={t("私人笔记")}
                        field="notes"
                        asset={activeAsset}
                        maxLength={8000}
                        onChange={changeAsset}
                      />
                      <AssetTextField
                        label={t("本人和 Agent 的分工")}
                        field="contribution"
                        asset={activeAsset}
                        maxLength={2000}
                        onChange={changeAsset}
                      />
                    </details>
                    <details className="studio-disclosure">
                      <summary>
                        <FolderOpen size={16} aria-hidden="true" />
                        {t("可复用的方法")}
                      </summary>
                      <AssetTextField
                        label={t("需要的输入")}
                        field="inputs"
                        asset={activeAsset}
                        maxLength={2000}
                        onChange={changeAsset}
                      />
                      <AssetTextField
                        label={t("执行步骤")}
                        field="steps"
                        asset={activeAsset}
                        maxLength={8000}
                        onChange={changeAsset}
                      />
                      <AssetTextField
                        label={t("预期交付")}
                        field="outputs"
                        asset={activeAsset}
                        maxLength={2000}
                        onChange={changeAsset}
                      />
                      <AssetTextField
                        label={t("新样例的验证记录")}
                        field="verification"
                        asset={activeAsset}
                        maxLength={2000}
                        onChange={changeAsset}
                      />
                      <MethodLibraryActions key={activeAsset.id + ':' + profile.revision + ':' + Boolean(draft)}
                        asset={activeAsset} revision={profile.revision} dirty={Boolean(draft)}
                        navigate={navigate} onChanged={onChanged} />
                    </details>
                    <details className="studio-disclosure">
                      <summary>
                        <Link2 size={16} aria-hidden="true" />
                        {t("项目依据")}
                      </summary>
                      {activeAsset.source_id && (
                        <p>
                          {t("关联依据")} ·{" "}
                          {activeAsset.source_title ? (
                            <Button
                              onClick={() =>
                                navigate({
                                  section: "assets",
                                  record: activeAsset.source_id,
                                })
                              }
                            >
                              {activeAsset.source_title}
                            </Button>
                          ) : (
                            t("来源已删除")
                          )}
                          {activeAsset.source_current_version !==
                            activeAsset.source_version && (
                            <strong> · {t("请重新核对依据")}</strong>
                          )}
                        </p>
                      )}
                      {!!activeAsset.evidence?.length && (
                        <ul>
                          {activeAsset.evidence.map((ref) => (
                            <li key={ref.id}>
                              {ref.title ? (
                                <Button
                                  onClick={() =>
                                    navigate({
                                      section: "assets",
                                      record: ref.id,
                                    })
                                  }
                                >
                                  {ref.title}
                                </Button>
                              ) : (
                                t("来源已删除")
                              )}{" "}
                              · {t("版本")} {ref.version}
                              {ref.current_version !== ref.version && (
                                <strong> · {t("请重新核对依据")}</strong>
                              )}
                              <Button
                                onClick={() =>
                                  changeAsset(activeAsset.id, {
                                    evidence: activeAsset.evidence.filter(
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
                          changeAsset(activeAsset.id, {
                            source_id: item.id,
                            source_version: item.version,
                            source_title: item.title,
                            source_current_version: item.version,
                            title: activeAsset.title || item.title,
                          })
                        }
                      />
                      <div className="toolbar-actions">
                        {activeAsset.source_id && (
                          <Button
                            onClick={() =>
                              changeAsset(activeAsset.id, {
                                source_id: "",
                                source_version: 0,
                              })
                            }
                          >
                            {t("解除关联")}
                          </Button>
                        )}
                      </div>
                    </details>
                    <div className="toolbar-actions studio-editor-actions">
                      {profile.assets.indexOf(activeAsset) > 0 && (
                        <Button
                          onClick={() => {
                            const assets = [...profile.assets];
                            const i = assets.indexOf(activeAsset);
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
                            assets: profile.assets.filter(
                              (it) => it.id !== activeAsset.id,
                            ),
                          })
                        }
                      >
                        {t("移除条目")}
                      </Button>
                    </div>
                  </section>
                </div>
              )}
              {profile.assets.length > 0 && (
                <button
                  type="button"
                  className="studio-next"
                  onClick={() => {
                    setEditing(true);
                    navigate({ section: "profile" });
                  }}
                >
                  {t("选择作品制作名片")}{" "}
                  <ArrowRight size={16} aria-hidden="true" />
                </button>
              )}
            </>
          ) : (
            <>
              <div className="studio-section-heading">
                <div>
                  <h2>{t("名片预览")}</h2>
                  <p>{t("选入简介的内容会出现在预览和导出的文本里。")}</p>
                </div>
                <Button
                  icon={PencilLine}
                  aria-expanded={editing}
                  onClick={() => setEditing(!editing)}
                >
                  {editing ? t("收起编辑") : t("编辑名片")}
                </Button>
              </div>
              <div
                className={
                  editing
                    ? "studio-profile-layout is-editing"
                    : "studio-profile-layout"
                }
              >
                <Preview profile={profile} />
                {editing && (
                  <section className="studio-card-editor">
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
                    {!profile.assets.length && (
                      <Button
                        icon={Plus}
                        onClick={() => navigate({ section: "assets" })}
                      >
                        {t("添加能力或作品")}
                      </Button>
                    )}
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
                )}
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
          <div className="toolbar-actions studio-save">
            {draft && (
              <Button
                variant="primary"
                icon={Check}
                busy={busy}
                disabled={!draft || Boolean(unnamedAsset)}
                onClick={save}
              >
                {t("保存到本机")}
              </Button>
            )}
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
                variant={draft ? "secondary" : "primary"}
                disabled={
                  !!draft ||
                  !(
                    profile.name ||
                    profile.bio ||
                    profile.assets.some((a) => a.selected)
                  )
                }
                data-export-kind="profile"
                onClick={download}
              >
                {t("导出当前简介")}
              </Button>
            )}
            {section === "profile" &&
              !draft &&
              !result.error &&
              !result.loading &&
              profile.assets.some(
                (asset) => asset.selected && asset.title.trim(),
              ) && (
                <Button
                  icon={ArrowRight}
                  onClick={() => navigate({ page: "exchange" })}
                >
                  {t("起草协作说明")}
                </Button>
              )}
            {unnamedAsset && (
              <Button
                onClick={() => {
                  focusNewAsset.current = true;
                  setSelectedAssetId(unnamedAsset.id);
                  if (section !== "assets") navigate({ section: "assets" });
                  if (
                    section === "assets" &&
                    unnamedAsset.id === activeAsset?.id
                  )
                    assetEditor.current?.focus();
                }}
              >
                {t("填写作品名称后保存")}
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
  initialTab = "memory",
}) {
  const section =
    [route.section, initialTab].find((id) =>
      tabs.some(([key]) => key === id),
    ) || "memory";
  const label = tabs.find(([id]) => id === section)[1];
  const [selectedAssetId, setSelectedAssetId] = useState("");
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
    <div className="personal-studio">
      <PageHeader title={t(label)} />
      <nav className="studio-tabs" aria-label={t("个人工作台")}>
        {tabs.map(([id, name, Icon]) => (
          <button
            key={id}
            type="button"
            aria-current={section === id ? "page" : undefined}
            onClick={() => navigate({ section: id })}
          >
            <Icon size={16} aria-hidden="true" />
            {t(name)}
          </button>
        ))}
      </nav>
      {route.record && (section === "memory" || section === "assets") ? (
        <ContentDetail
          key={route.record}
          id={route.record}
          projects={projects}
          revision={revision}
          onChanged={onChanged}
          onBack={() => navigate({ record: null })}
          onProject={(project) => navigate({ page: "projects", project })}
        />
      ) : section === "memory" ? (
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
      ) : (
        <ProfileEditor
          section={section}
          projects={projects}
          revision={revision}
          draftStore={draftStore}
          selectedAssetId={selectedAssetId}
          setSelectedAssetId={setSelectedAssetId}
          sourceProject={
            projects.some((project) => project.id === route.project)
              ? route.project
              : ""
          }
          navigate={navigate}
          onChanged={onChanged}
        />
      )}
    </div>
  );
}
