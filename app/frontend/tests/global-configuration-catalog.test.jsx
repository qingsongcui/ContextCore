// @vitest-environment jsdom
import React,{act} from 'react';
import {createRoot} from 'react-dom/client';
import {it,expect,beforeEach,afterEach,vi} from 'vitest';
const h=vi.hoisted(()=>({mutate:vi.fn(), paths:[], rows:{}}));
vi.mock('../src/lib/useResource.js',()=>({useResource(path){h.paths.push(path);const collection=path.split('/')[3].split('?')[0];return {data:{items:h.rows[collection] || []},reload(){}};}}));
vi.mock('../src/lib/api.js',()=>({mutate:h.mutate}));
import GlobalConfigurationCatalog from '../src/components/agent-management/GlobalConfigurationCatalog.jsx';
import {setLang} from '../src/lib/i18n.jsx';
const hosts=['codex','claude-code'].map(id=>({id,name:id,shared_configuration:id==='codex'}));
let root;
const button=text=>[...document.querySelectorAll('button')].find(el=>el.textContent===text);
const click=async el=>act(async()=>el.click());
beforeEach(async()=>{
  HTMLDialogElement.prototype.showModal = function () {this.open=true;};
  HTMLDialogElement.prototype.close = function () {this.open=false;};
  setLang('zh'); globalThis.IS_REACT_ACT_ENVIRONMENT=true;
  h.rows={providers:[{id:'p',name:'Provider',version:3,inherited_agent_ids:['codex']}],
    models:[{id:'m',name:'Example',provider_id:'p',model_id:'example-model',version:1}],
    accounts:[{id:'a',kind:'oauth',name:'Sign in',version:2}],
    bundles:[{id:'b',name:'Weekly',version:5,host_ids:['codex']}]};
  h.mutate.mockReset();h.paths=[];
  document.body.innerHTML='<div id="root"></div>';root=createRoot(document.querySelector('#root'));
  await act(async()=>root.render(<GlobalConfigurationCatalog hosts={hosts} revision={1}/>));
});
afterEach(async()=>act(async()=>root.unmount()));
it('keeps legacy provider membership and assigns multiple targets with one versioned metadata write',async()=>{
  await click(button('编辑定义'));
  const inputs=[...document.querySelectorAll('.global-checkboxes input')];
  expect(inputs.map(el=>el.checked)).toEqual([true,false]);
  await click(inputs[1]); await click(button('保存目录定义'));
  expect(h.mutate).toHaveBeenCalledTimes(1);
  expect(h.mutate).toHaveBeenCalledWith('/api/manage/providers/p','PATCH',{expected_version:3,agent_ids:['codex','claude-code']});
});
it('follows a model to its provider instead of writing ineffective model host overrides',async()=>{
  await click(button('模型'));await click(button('编辑定义'));expect(button('全部选中')).toBeUndefined();
  await click(button('查看所属服务商'));
  expect(h.paths.at(-1)).toContain('providers?');expect(h.paths.at(-1)).toContain('ids=p');expect(h.mutate).not.toHaveBeenCalled();
});
it('updates bundle directory assignment without changing execution scope or activating profiles',async()=>{
  await click(button('项目配置包'));await click(button('编辑定义'));
  await click(document.querySelectorAll('.global-host-picker')[0].querySelectorAll('input')[1]);await click(button('保存目录定义'));
  expect(h.mutate).toHaveBeenCalledWith('/api/manage/bundles/b','PATCH',{expected_version:5,agent_ids:['codex','claude-code']});
});
it('keeps an account conflict visible and lets the user reload without retrying a stale write',async()=>{
  h.mutate.mockRejectedValueOnce(Object.assign(new Error('Conflict'),{status:409}));
  await click(button('账号'));await click(button('编辑定义'));await click(button('全部选中'));await click(button('保存目录定义'));
  expect(h.mutate).toHaveBeenCalledWith('/api/manage/accounts/a','PATCH',{expected_version:2,agent_ids:['codex','claude-code']});
  expect(document.querySelector('dialog')).toBeTruthy();await click(button('放弃编辑并读取最新目录'));
  expect(h.mutate).toHaveBeenCalledTimes(1);expect(document.querySelector('dialog')).toBeNull();
});
