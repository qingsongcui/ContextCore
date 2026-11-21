#!/bin/zsh -f
# Open the global library with real home-directory host paths only on explicit launch.
set -eu
script_dir=${0:A:h}
if [[ -x "$script_dir/service/senrelay-service" ]]; then
  resources=$script_dir
else
  resources="$script_dir/Senrelay.app/Contents/Resources"
fi
service="$resources/service/senrelay-service"
if [[ ! -x "$service" ]]; then
  print -u2 "请将此启动器放在本次 Senrelay.app 旁边再打开。"
  exit 1
fi
data_dir="$HOME/Library/Application Support/SenrelayHostWorkspaceTest"
print "Senrelay · 本机 Agent 配置工作台"
print "独立数据目录  $data_dir"
print "宿主配置示例  ${CODEX_HOME:-$HOME/.codex}/config.toml"
print "目标文件以每次分发前的预览为准。确认应用后，工作台会写入你所选 Agent 的实际配置。"
print "本机网页  http://127.0.0.1:19446/?page=agents&section=library"
print "输入 START 并回车启动。服务在当前终端前台运行，按 Ctrl-C 结束。"
IFS= read -r answer || exit 0
[[ "$answer" == START ]] || exit 0
exec "$service" -m app.cli serve --port 19446 --data-dir "$data_dir"
