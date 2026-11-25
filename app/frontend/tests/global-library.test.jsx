// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({ mutate: vi.fn(), matrix: null, paths: [] }));
vi.mock('../src/lib/api.js', async original => ({...await original(), mutate:h.mutate}));
vi.mock('../src/lib/useResource.js', async original => ({...await original(), useResource(path) {
  if (path) h.paths.push(path);
  return {data:path?.includes('shared-resource-matrix') ? h.matrix : null, loading:false, error:null, reload(){}};
}}));
import { GlobalLibrary } from '../src/components/agent-management/GlobalLibrary.jsx';
import { setLang } from '../src/lib/i18n.jsx';

const hosts = [
  {id:'codex',name:'Codex',shared_configuration:true}, {id:'omp',name:'Oh My Pi'},
  {id:'claude-code',name:'Claude Code'},
].map(host => ({...host,capabilities:{mcp:true,skills:true}}));
const item = (id, version, kind='mcp') => ({id,version,kind,name:id, cells:{
  codex:{status:'verified',applied_version:version}, omp:{status:'not_managed'},
  'claude-code':{status:'verified',applied_version:version},
}});
let root;
const button = text => [...document.querySelectorAll('button')].find(el => el.textContent === text);
const click = async el => act(async () => {expect(el).toBeTruthy();el.click();});
const render = async props => act(async () => root.render(<GlobalLibrary hosts={hosts} revision={0} {...props}/>));
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML='<div id="root"></div>';
  root=createRoot(document.querySelector('#root'));
  setLang('zh');
  h.paths=[];h.mutate.mockReset();h.mutate.mockRejectedValue(new Error('test'));
  h.matrix={items:[item('First',2),item('Second',4)],summary:true,offset:0,limit:15,total:2,has_more:false};
});
afterEach(async () => {await act(async () => root.unmount());});

it('previews one versioned batch from multi-select, normalizes shared targets and excludes changed native files', async () => {
  await render();
  const url = new URL(h.paths.find(path => path.includes('shared-resource-matrix')), 'http://localhost');
  expect(url.searchParams.get('scope')).toBe('global');
  expect(url.searchParams.get('host_ids').split(',')).toEqual(hosts.map(host => host.id));
  await click(button('全选本页资源'));await click(button('全选可用助手'));
  expect([...document.querySelectorAll('.global-host-choice')].map(el => el.textContent)).toEqual([
    'Codex2/2 项可应用','Oh My Pi2/2 项可应用','Claude Code2/2 项可应用',
  ]);
  expect(document.querySelectorAll('.global-host-choice input:checked')).toHaveLength(3);
  await click(button('预览批量更改'));
  expect(h.mutate).toHaveBeenCalledTimes(1);
  expect(h.mutate).toHaveBeenCalledWith('/api/manage/shared-batches','POST',{
    operation:'install', targets:[
      {host_id:'codex',asset_versions:{First:2,Second:4}},
      {host_id:'omp',asset_versions:{First:2,Second:4}},
      {host_id:'claude-code',asset_versions:{First:2,Second:4}},
    ],
  });
  await click(button('Skills'));
  expect(document.querySelectorAll('.global-host-choice input:checked')).toHaveLength(0);
  expect(button('预览批量更改').disabled).toBe(true);
});

it('finds and selects a deep-linked Skill from its actual versioned matrix row, with explicit target selection', async () => {
  h.matrix.items=[item('skill-id',7,'skill')];
  const onLocation=vi.fn();
  await render({initialKind:'skill',focusedRecord:'skill-id',onLocation});
  const url = new URL(h.paths.find(path => path.includes('shared-resource-matrix')), 'http://localhost');
  expect(url.searchParams.get('kind')).toBe('skill');
  expect(url.searchParams.get('asset_id')).toBe('skill-id');
  expect(document.querySelector('.global-asset-row input').checked).toBe(true);
  expect(document.querySelectorAll('.global-host-choice input:checked')).toHaveLength(0);
  expect(h.mutate).not.toHaveBeenCalled();
  await click(button('查看此分类全部资源'));
  expect(onLocation).toHaveBeenCalledWith('skill');
  expect(document.querySelector('.global-asset-row input').checked).toBe(false);
});

it('never selects a missing deep-linked definition from an unrelated row',async () => {
  await render({initialKind:'skill',focusedRecord:'not-here'});
  expect(document.querySelectorAll('.global-asset-row input:checked')).toHaveLength(0);
  expect(button('预览批量更改').disabled).toBe(true);
});

it('mixes resources from different pages and deduplicates physical host targets separately for each kind', async () => {
  const surfaces = [
    {id:'antigravity',name:'Antigravity'},
    {id:'opencode',name:'OpenCode'},
  ].map(host => ({...host, capabilities:{mcp:true,skills:host.id === 'antigravity',prompts:host.id === 'antigravity'},
    paths:{mcp_path:`/${host.id}/tools.json`, skill_dir:`/${host.id}/skills`}}));
  function mixedItem(id,kind) {
    return {id,kind,name:id,version:2,cells:Object.fromEntries(surfaces.map(host => [host.id,{status:'not_managed'}]))};
  }
  h.matrix = {items:[mixedItem('Tools','mcp'),mixedItem('Method','skill')],summary:true,total:3,limit:2,offset:0,has_more:true};
  await render({hosts:surfaces});
  expect(button('全部资源').getAttribute('aria-pressed')).toBe('true');
  expect(h.paths.some(path=>path.includes('kind=all'))).toBe(true);
  await click(button('全选本页资源'));
  await click(button('下一页'));
  h.matrix = {...h.matrix,items:[mixedItem('Instruction','prompt')],offset:2,has_more:false};
  await render({hosts:surfaces});
  await click(button('全选本页资源')); await click(button('全选可用助手'));
  expect(document.querySelector('.global-selection').textContent).toContain('已选 3 项资源');
  expect([...document.querySelectorAll('.global-host-choice')].find(label => label.textContent.includes('OpenCode')).textContent).toContain('1/3');
  await click(button('预览批量更改'));
  expect(h.mutate).toHaveBeenCalledWith('/api/manage/shared-batches','POST',{
    operation:'install',targets:[
      {host_id:'antigravity',asset_versions:{Tools:2,Method:2,Instruction:2}},
      {host_id:'opencode',asset_versions:{Tools:2}},
    ],
  });
});
