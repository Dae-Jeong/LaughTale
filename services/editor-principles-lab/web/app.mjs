import {initial, edit, undo, redo, add, move, textEdit, same} from './core.mjs';
const $ = id => document.getElementById(id);
let state = null, revision = null, saved = null, saving = false, pending = null, drag = null;
const dirty = () => state && !same(state.present, saved);
function status(message, error = false) { $('status').textContent = message; $('status').dataset.kind = error ? 'error' : 'ok'; }
async function request(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  $('request').textContent = `${options?.method || 'GET'} ${path}\nHTTP ${response.status}\n${JSON.stringify(body, null, 2)}`;
  if (!response.ok) throw Object.assign(new Error(body.error), {status: response.status, body});
  return body;
}
function render() {
  const doc = state?.present;
  $('save').disabled = !doc || saving;
  $('create').disabled = saving;
  $('documents').disabled = saving;
  $('reload').disabled = !doc || saving;
  $('adopt').disabled = saving;
  for (const id of ['add-text', 'add-rectangle', 'title']) $(id).disabled = !doc;
  $('undo').disabled = !state?.past.length;
  $('redo').disabled = !state?.future.length;
  $('revision').textContent = `서버 revision ${revision ?? '—'} · ${saving ? '저장 중' : dirty() ? '미저장 변경' : '저장됨'}`;
  if (document.activeElement !== $('title')) $('title').value = doc?.title || '';
  const option = [...$('documents').options].find(option => option.value === doc?.id);
  if (option) option.textContent = doc.title;
  const selected = doc?.elements.find(e => e.id === state.selection);
  $('text').disabled = selected?.type !== 'text';
  if (document.activeElement !== $('text')) $('text').value = selected?.text || '';
  $('selection').textContent = selected ? `${selected.type} · x ${Math.round(selected.x)}, y ${Math.round(selected.y)}` : '선택 없음';
  $('state').replaceChildren();
  for (const [key, value] of Object.entries({'요소 수': doc?.elements.length ?? 0, 'undo / redo': `${state?.past.length ?? 0} / ${state?.future.length ?? 0}`, '로컬 변경': dirty() ? '있음' : '없음', '서버 revision': revision ?? '—'})) {
    const dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = key; dd.textContent = value; $('state').append(dt, dd);
  }
  $('model').textContent = JSON.stringify(doc, null, 2);
  draw(doc);
}
function svg(tag, attrs) { const node = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [k,v] of Object.entries(attrs)) node.setAttribute(k,v); return node; }
function draw(doc) {
  const canvas = $('canvas'); canvas.replaceChildren();
  if (!doc) return;
  canvas.setAttribute('viewBox', `0 0 ${doc.width} ${doc.height}`);
  for (const e of doc.elements) {
    const group = svg('g', {'data-id':e.id, 'aria-label': e.type === 'text' ? e.text : '사각형', role:'img'});
    group.append(svg('rect', {x:e.x,y:e.y,width:e.width,height:e.height,fill:e.type === 'rectangle' ? e.fill : 'transparent',stroke:state.selection === e.id ? '#a66a18' : 'none','stroke-width':2}));
    if (e.type === 'text') {
      const foreign = svg('foreignObject',{x:e.x,y:e.y,width:e.width,height:e.height});
      const div = document.createElement('div'); div.textContent = e.text; div.style.cssText='font-size:24px;line-height:1.3;overflow:hidden;overflow-wrap:anywhere;height:100%;pointer-events:none'; foreign.append(div); group.append(foreign);
    }
    canvas.append(group);
  }
}
function change(doc) { state = edit(state,doc); render(); }
function openRecord(record) { state = initial(record.document); revision = record.revision; saved = structuredClone(record.document); pending = null; $('comparison').hidden = true; location.hash = record.document.id; render(); status('문서를 불러왔습니다. 요소를 추가해 보세요.'); }
async function list() { const body = await request('/api/documents'); $('documents').replaceChildren(new Option('문서를 선택하세요', '')); for (const d of body.documents) $('documents').add(new Option(d.title, d.id)); $('documents').value = state?.present.id || ''; }
async function inspect(id = state?.present.id) {
  if (!id) return;
  const record = await request(`/api/documents/${id}`);
  if (!state || (!dirty() && id !== state.present.id)) openRecord(record);
  else { pending = record; $('comparison').hidden = false; $('remote').textContent = `서버 revision ${record.revision}\n${JSON.stringify(record.document,null,2)}`; status('서버 내용을 확인했습니다. 내 작업은 그대로 보존됩니다.'); }
  render();
}
function safe(fn) { return async () => { try { await fn(); } catch(e) {status(`요청 실패: ${e.message}. 내 작업은 유지됩니다.`,true);} }; }
$('create').onclick = safe(async () => { if (dirty() && !confirm('미저장 변경을 버리고 새 문서를 만들까요?')) return; openRecord(await request('/api/documents',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})); await list(); });
$('refresh').onclick = safe(list);
$('documents').onchange = safe(async () => { await inspect($('documents').value); });
$('reload').onclick = safe(() => inspect());
$('adopt').onclick = () => { if (!saving && pending && (!dirty() || confirm('내 미저장 변경과 undo 이력을 버리고 서버 내용으로 교체할까요?'))) {openRecord(pending); $('documents').value = state.present.id;} };
$('dismiss').onclick = () => {pending=null; $('comparison').hidden=true; $('documents').value=state?.present.id || '';};
$('download').onclick = () => {const url=URL.createObjectURL(new Blob([JSON.stringify(state.present,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='local-document.json';a.click();URL.revokeObjectURL(url);};
$('save').onclick = async () => {
  if (!state || saving) return;
  const snapshot = structuredClone(state.present), expected = revision;
  const fault = $('fault').value; $('fault').value = 'none'; saving = true; render(); status('저장 중입니다. 편집은 계속할 수 있습니다.');
  try {
    const record = await request(`/api/documents/${snapshot.id}`,{method:'PUT',headers:{'Content-Type':'application/json','X-Lab-Fault':fault},body:JSON.stringify({document:snapshot,expected_revision:expected})});
    if (state.present.id === snapshot.id) {revision=record.revision;saved=snapshot;status(dirty() ? '저장은 완료됐습니다. 저장 중 추가한 변경은 아직 미저장입니다.' : '저장 완료. 서버와 내 문서가 같습니다.');}
  } catch(e) {
    if(e.status===409){pending=e.body;$('comparison').hidden=false;$('remote').textContent=`서버 revision ${e.body.revision}\n${JSON.stringify(e.body.document,null,2)}`;status('409 충돌: 다른 탭이 먼저 저장했습니다. 내 변경을 보존했습니다. 서버와 비교하세요.',true);}
    else if(e.status){status(`${e.status} 저장 실패: 내 변경을 보존했습니다. 다시 저장할 수 있습니다.`,true);}
    else {status('결과 불명: 응답을 받지 못했습니다. 서버에 기록됐을 수 있습니다. 서버 확인으로 비교하세요.',true);$('request').textContent=`PUT /api/documents/${snapshot.id}\n응답 없음 · expected_revision ${expected}\n서버 기록 여부를 이 응답만으로 알 수 없음`;}
  } finally {saving=false;render();}
};
for (const type of ['text','rectangle']) $('add-'+type).onclick = () => {if(!state)return; const doc=add(state.present,type);change(doc);state.selection=doc.elements.at(-1)?.id;render();};
$('title').onchange = () => { if(state){const title=$('title').value.trim(); if(title)change({...state.present,title});else{$('title').value=state.present.title;status('제목은 비울 수 없습니다.',true);}} };
$('text').onchange = () => {if(state)change(textEdit(state.present,state.selection,$('text').value));};
$('undo').onclick = () => {if(state){state=undo(state);render();}};
$('redo').onclick = () => {if(state){state=redo(state);render();}};
function point(event) { const p=new DOMPoint(event.clientX,event.clientY).matrixTransform($('canvas').getScreenCTM().inverse());return p; }
$('canvas').onpointerdown = event => {
  if(!state || event.button !== 0)return;
  const id=event.target.closest('[data-id]')?.dataset.id;state.selection=id || null;
  if(id){const e=state.present.elements.find(e=>e.id===id);drag={pointer:event.pointerId,start:point(event),element:e,base:state.present};$('canvas').setPointerCapture(event.pointerId);}
  $('canvas').focus();render();
};
$('canvas').onpointermove = event => {if(drag && drag.pointer===event.pointerId){const p=point(event);state={...state,present:move(drag.base,drag.element.id,drag.element.x+p.x-drag.start.x,drag.element.y+p.y-drag.start.y)};render();}};
function finish(cancel=false){if(!drag)return;const next=state.present;state={...state,present:drag.base};if(!cancel)state=edit(state,next);drag=null;render();}
$('canvas').onpointerup=()=>finish();$('canvas').onpointercancel=()=>finish(true);$('canvas').onlostpointercapture=()=>finish(true);
document.addEventListener('keydown',event=>{
  if(!state || ['INPUT','TEXTAREA','SELECT'].includes(event.target.tagName))return;
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'){event.preventDefault();state=event.shiftKey?redo(state):undo(state);render();return;}
  if(event.target!==$('canvas'))return;
  const offsets={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]},offset=offsets[event.key];
  const e=state.present.elements.find(e=>e.id===state.selection);if(offset && e){event.preventDefault();const step=event.shiftKey?10:1;change(move(state.present,e.id,e.x+offset[0]*step,e.y+offset[1]*step));}
});
window.addEventListener('beforeunload',event=>{if(dirty()){event.preventDefault();event.returnValue='';}});
render();
safe(async()=>{await list();if(location.hash.length>1)openRecord(await request(`/api/documents/${location.hash.slice(1)}`));$('documents').value=state?.present.id||'';})();
