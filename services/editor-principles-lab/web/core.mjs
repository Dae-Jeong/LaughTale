export const HISTORY_LIMIT = 50;
export const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
export const initial = document => ({past: [], present: structuredClone(document), future: [], selection: null});
export function edit(state, document) {
  if (same(state.present, document)) return state;
  return {...state, past: [...state.past, state.present].slice(-HISTORY_LIMIT), present: structuredClone(document), future: []};
}
export function undo(s) {
  return s.past.length ? {...s, past: s.past.slice(0, -1), present: s.past.at(-1), future: [s.present, ...s.future], selection: null} : s;
}
export function redo(s) {
  return s.future.length ? {...s, past: [...s.past, s.present].slice(-HISTORY_LIMIT), present: s.future[0], future: s.future.slice(1), selection: null} : s;
}
export function add(doc, type, id = crypto.randomUUID()) {
  if (doc.elements.length >= 200 || !['text', 'rectangle'].includes(type)) return doc;
  const element = {id, type, x: 60, y: 60, width: 200, height: 64, ...(type === 'text' ? {text: '나의 첫 문장'} : {fill: '#74b5a5'})};
  return {...doc, elements: [...doc.elements, element]};
}
export function move(doc, id, x, y) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return doc;
  return {...doc, elements: doc.elements.map(e => e.id === id ? {...e, x: Math.max(0, Math.min(doc.width - e.width, x)), y: Math.max(0, Math.min(doc.height - e.height, y))} : e)};
}
export function textEdit(doc, id, text) {
  return {...doc, elements: doc.elements.map(e => e.id === id && e.type === 'text' ? {...e, text: text.slice(0, 500)} : e)};
}
