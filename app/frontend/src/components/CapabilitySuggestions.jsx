import { useEffect, useState } from "react";
import { FolderSearch } from "lucide-react";
import { Button, ErrorNotice } from "./ui.jsx";
import { mutate } from "../lib/api.js";
import { t } from "../lib/i18n.jsx";

export function CapabilitySuggestions({
  projects,
  profile,
  change,
  makeAsset,
  onAdopt,
  sourceProject,
  onProjects,
}) {
  const [selected, setSelected] = useState(() =>
    sourceProject ? [sourceProject] : [],
  );
  const [open, setOpen] = useState(Boolean(sourceProject));
  const [offset, setOffset] = useState(0);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let current = true;
    setResult(null);
    setError(null);
    if (selected.length) {
      mutate("/api/workbench/candidates", "POST", {
        project_ids: selected,
        offset,
      })
        .then((data) => {
          if (current) setResult(data);
        })
        .catch((failure) => {
          if (current) setError(failure);
        });
    }
    return () => {
      current = false;
    };
  }, [selected, offset]);
  const hidden = profile.hidden_topics || [];
  const items = (result?.items || []).filter(
    (item) =>
      !hidden.includes(item.topic) &&
      !profile.assets.some((a) => a.candidate_topic === item.topic),
  );
  return (
    <details
      className="studio-suggestions"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <FolderSearch size={17} aria-hidden="true" />
        {t("从项目整理能力线索")}
      </summary>
      <p>{t("选择项目，对照已有作品和经验，再写下你或 Agent 承担的部分。")}</p>
      <div className="studio-project-choices">
        {!projects.length && (
          <>
            <small>{t("加入项目后，可以在这里选择整理范围。")}</small>
            <Button onClick={onProjects}>{t("打开项目")}</Button>
          </>
        )}
        {projects.map((p) => (
          <label key={p.id} className="check-label">
            <input
              type="checkbox"
              checked={selected.includes(p.id)}
              onChange={(e) => {
                setOffset(0);
                setSelected(
                  e.target.checked
                    ? [...selected, p.id]
                    : selected.filter((id) => id !== p.id),
                );
              }}
            />
            {p.name}
          </label>
        ))}
      </div>
      <ErrorNotice error={error} />
      {!!selected.length && !result && !error && (
        <p role="status">{t("正在读取")}</p>
      )}
      {result && !items.length && <p>{t("本页没有新的能力线索。")}</p>}
      {items.map((item) => (
        <section className="personal-asset" key={item.topic}>
          <strong>{item.evidence[0].title}</strong>
          <p>
            {t("来自 {projects} 个项目、{samples} 项资料或经验。", {
              projects: item.projects,
              samples: item.samples,
            })}
          </p>
          <details className="studio-examples">
            <summary>{t("查看项目依据")}</summary>
            <ul>
              {item.evidence.map((ref) => (
                <li key={ref.id}>
                  {ref.title}
                  <small>
                    {
                      projects.find((project) => project.id === ref.project_id)
                        ?.name
                    }
                  </small>
                </li>
              ))}
            </ul>
          </details>
          <Button
            disabled={profile.assets.length >= 200}
            onClick={() =>
              onAdopt({
                ...makeAsset(),
                title: item.evidence[0].title.slice(0, 200),
                candidate_topic: item.topic,
                evidence: item.evidence.map((ref) => ({
                  ...ref,
                  current_version: ref.version,
                })),
              })
            }
          >
            {t("采用为能力草稿")}
          </Button>
          <Button
            onClick={() =>
              change({ ...profile, hidden_topics: [...hidden, item.topic] })
            }
          >
            {t("隐藏线索")}
          </Button>
        </section>
      ))}
      {offset > 0 && (
        <Button onClick={() => setOffset(offset - 12)}>{t("上一页")}</Button>
      )}
      {result?.more && (
        <Button onClick={() => setOffset(offset + 12)}>{t("下一页")}</Button>
      )}
      {!!hidden.length && (
        <Button onClick={() => change({ ...profile, hidden_topics: [] })}>
          {t("恢复隐藏的线索")}
        </Button>
      )}
    </details>
  );
}
