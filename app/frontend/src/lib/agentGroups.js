// Single frontend source for the six canonical product Agents.
export const AGENT_LABELS = {
  codex: 'Codex',
  omp: 'Oh My Pi',
  cursor: 'Cursor',
  antigravity: 'Antigravity',
  'claude-code': 'Claude Code',
  opencode: 'OpenCode',
};

export const AGENTS = AGENT_LABELS;

export const isPrimaryAgent = id => Object.hasOwn(AGENT_LABELS, id);

export const agentGroup = id => id;

export const nativeTargetName = host =>
  host?.label || host?.name || AGENT_LABELS[host?.id] || host?.id || '';

export const agentGroupName = host => AGENT_LABELS[host?.id] || nativeTargetName(host);

export function primaryAgents(hosts) {
  return hosts.filter(host => isPrimaryAgent(host.id));
}

export function configurationTarget(_hosts, id) {
  return id;
}

export function configurationChoices(hosts) {
  return hosts.filter(host => isPrimaryAgent(host.id));
}