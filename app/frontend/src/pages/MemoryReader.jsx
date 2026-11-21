import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { Button, ResourceState, formatTime } from "../components/ui.jsx";
import { t } from "../lib/i18n.jsx";
import { queryString, useResource } from "../lib/useResource.js";

const GROUPS = [
  ["decision", "当前决定"],
  ["background", "项目背景"],
  ["preference", "工作约定"],
  ["lesson", "经验与教训"],
  ["knowledge", "保留的知识"],
  ["personal", "本项目采用的个人约定"],
  ["shared", "跨项目共享的知识"],
];

export function MemoryReader({
  projectId,
  revision,
  onOpen,
  onManage,
  onTasks,
  onAdd,
}) {
  const [offset, setOffset] = useState(0);
  const result = useResource(
    "/api/workbench/memories" +
      queryString({ project_id: projectId, offset, limit: 40 }),
    revision,
  );
  const items = result.data?.items || [];
  return (
    <section className="memory-reader">
      <div className="reader-heading">
        <div>
          <h2>{projectId ? t("项目现在怎么做") : t("我的工作约定")}</h2>
          <p>
            {projectId
              ? t(
                  "当前采用的决定、约定和经验。展开一条记忆可以查看依据和变更。",
                )
              : t(
                  "跨项目适用的工作方式。在某个项目有例外时，优先采用项目约定。",
                )}
          </p>
        </div>
        <div className="toolbar-actions">
          {onTasks && <Button onClick={onTasks}>{t("当前任务")}</Button>}
          {onManage && <Button onClick={onManage}>{t("管理记忆")}</Button>}
          {onAdd && <Button onClick={onAdd}>{t("添加个人约定")}</Button>}
        </div>
      </div>
      <ResourceState resource={result}>
        {!items.length && (
          <p className="section-empty">{t("这里还没有生效的记忆。")}</p>
        )}
        {GROUPS.map(([key, label]) => {
          const rows = items.filter(
            (item) => (item.section || item.category) === key,
          );
          if (!rows.length) return null;
          return (
            <section className="reader-group" key={key}>
              <h3>{t(label)}</h3>
              {rows.map((item) => (
                <details className="reader-item" key={item.id}>
                  <summary>{item.heading}</summary>
                  {item.content &&
                    !item.content.includes(item.heading) &&
                    !item.sources?.some(
                      (source) => source.quote === item.content,
                    ) && <p>{item.content}</p>}
                  {(item.sources || []).map((source, index) => (
                    <blockquote key={source.id || index}>
                      {source.quote && <p>{source.quote}</p>}
                      <small>
                        {source.deleted
                          ? t("来源已删除")
                          : source.title || t("来源")}{" "}
                        · {t("更新于")} {formatTime(item.updated_at)}
                      </small>
                    </blockquote>
                  ))}
                  {item.previous?.map((old) => (
                    <Button key={old.id} onClick={() => onOpen(old.id)}>
                      {t("此前采用")} · {old.title}
                    </Button>
                  ))}
                  <Button onClick={() => onOpen(item.id)}>
                    {t("详情与纠正")} <ArrowRight size={14} />
                  </Button>
                </details>
              ))}
            </section>
          );
        })}
        {(offset > 0 || offset + 40 < result.data?.total) && (
          <div className="toolbar-actions">
            <Button
              disabled={!offset}
              onClick={() => setOffset(Math.max(0, offset - 40))}
            >
              {t("上一页")}
            </Button>
            <Button
              disabled={offset + 40 >= result.data?.total}
              onClick={() => setOffset(offset + 40)}
            >
              {t("下一页")}
            </Button>
          </div>
        )}
      </ResourceState>
    </section>
  );
}
