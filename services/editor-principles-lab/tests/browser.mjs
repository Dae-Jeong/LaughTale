// Run with installed desktop Chrome; test server uses an isolated temporary directory.
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
import {spawn} from 'node:child_process';
import {mkdtemp,rm,writeFile,mkdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
const temp=await mkdtemp(join(tmpdir(),'editor-browser-'));
const results=process.env.LAB_RESULTS_DIR || 'results';
const server=spawn(process.env.LAB_PYTHON || 'python3',['-u','server.py','--port','0','--data-dir',temp],{stdio:['ignore','pipe','pipe']});
const base=await new Promise((resolve,reject)=>{server.stdout.on('data',d=>{const m=d.toString().match(/http:\/\/127\.0\.0\.1:\d+/);if(m)resolve(m[0]);});server.on('error',reject);server.on('exit',code=>reject(new Error(`server exit ${code}`)));});
const browser=await chromium.launch({channel:'chrome',headless:true});
const context=await browser.newContext({viewport:{width:1440,height:1000}});
const errors=[],passed=[];
const p=await context.newPage();
context.on('page',page=>{page.on('dialog',d=>d.accept());page.on('pageerror',e=>errors.push(e.message));});
p.on('dialog',d=>d.accept());p.on('pageerror',e=>errors.push(e.message));
const model=async page=>JSON.parse(await page.locator('#model').textContent());
const status=async(page,text)=>page.waitForFunction(text=>document.querySelector('#status').textContent.includes(text),text);
const title=async(page,value)=>{await page.locator('#title').fill(value);await page.locator('#title').press('Tab');};
async function check(name,fn){await fn();passed.push(name);console.log('PASS',name);}
try {
 await p.goto(base);
 await check('create text rectangle; edit title/text; keyboard movement',async()=>{
  await p.locator('#create').click();await status(p,'불러왔습니다');
  await title(p,'원리를 배우는 작은 문서');
  await p.locator('#add-text').click();await p.locator('#text').fill('변경 → 이력 → 저장');await p.locator('#text').press('Tab');
  await p.locator('#add-rectangle').click();await p.locator('#canvas').focus();await p.keyboard.press('Shift+ArrowRight');
  assert.equal((await model(p)).elements[1].x,70);assert.equal((await model(p)).elements[0].text,'변경 → 이력 → 저장');
 });
 await check('pointer drag is one undo entry; redo restores movement',async()=>{
  const before=await model(p),box=await p.locator('#canvas').boundingBox();
  const x=box.x+100*box.width/800,y=box.y+85*box.height/500;
  await p.mouse.move(x,y);await p.mouse.down();await p.mouse.move(x+190,y+130,{steps:12});await p.mouse.up();
  const after=await model(p);assert.ok(after.elements[1].x>before.elements[1].x+100);
  await p.locator('#undo').click();assert.deepEqual(await model(p),before);
  await p.locator('#redo').click();assert.deepEqual(await model(p),after);
 });
 await check('pointer cancellation restores document and history',async()=>{
  const before=await model(p),history=await p.locator('#state').textContent();
  const box=await p.locator('#canvas').boundingBox(),e=before.elements[1];
  const x=box.x+(e.x+20)*box.width/800,y=box.y+(e.y+20)*box.height/500;
  await p.mouse.move(x,y);await p.mouse.down();await p.mouse.move(x+30,y+30);
  await p.locator('#canvas').dispatchEvent('pointercancel');await p.mouse.up();
  assert.deepEqual(await model(p),before);assert.equal(await p.locator('#state').textContent(),history);
 });
 await check('save and reload restore exact model',async()=>{await p.locator('#save').click();await status(p,'저장 완료');const before=await model(p);await p.reload();await status(p,'불러왔습니다');assert.deepEqual(await model(p),before);});
 const b=await context.newPage();await b.goto(p.url());await status(b,'불러왔습니다');
 await check('two tabs: 409 preserves local changes',async()=>{
  await title(p,'탭 A 먼저 저장');await p.locator('#save').click();await status(p,'저장 완료');
  await title(b,'탭 B 보존할 변경');await b.locator('#save').click();await status(b,'409 충돌');
  assert.equal((await model(b)).title,'탭 B 보존할 변경');assert.ok((await b.locator('#remote').textContent()).includes('탭 A 먼저 저장'));
 });
 await check('server inspect preserves local until explicit adoption',async()=>{await b.locator('#reload').click();await status(b,'확인했습니다');assert.equal((await model(b)).title,'탭 B 보존할 변경');await b.locator('#adopt').click();assert.equal((await model(b)).title,'탭 A 먼저 저장');});
 await check('503 retains local; retry succeeds',async()=>{await title(p,'실패 후에도 남는 문장');await p.locator('#fault').selectOption('before');await p.locator('#save').click();await status(p,'503 저장 실패');assert.equal((await model(p)).title,'실패 후에도 남는 문장');await p.locator('#save').click();await status(p,'저장 완료');});
 await check('lost response shows unknown; server committed',async()=>{await title(p,'응답은 없어도 기록은 남는다');await p.locator('#fault').selectOption('lost');await p.locator('#save').click();await status(p,'결과 불명');assert.equal((await model(p)).title,'응답은 없어도 기록은 남는다');await p.locator('#reload').click();await status(p,'확인했습니다');assert.ok((await p.locator('#remote').textContent()).includes('응답은 없어도 기록은 남는다'));await p.locator('#adopt').click();});
 await check('late save response preserves edits; document switch disabled',async()=>{
  let release;const gate=new Promise(r=>release=r);let captured;const seen=new Promise(r=>captured=r);
  await p.route('**/api/documents/*',async route=>{if(route.request().method()==='PUT'){const response=await route.fetch();captured();await gate;await route.fulfill({response});}else await route.continue();});
  await title(p,'저장에 보낸 제목');await p.locator('#save').click();await seen;
  assert.equal(await p.locator('#create').isDisabled(),true);assert.equal(await p.locator('#documents').isDisabled(),true);
  await title(p,'저장 중 새 편집');release();await status(p,'추가한 변경');assert.equal((await model(p)).title,'저장 중 새 편집');assert.ok((await p.locator('#revision').textContent()).includes('미저장'));
  await p.unroute('**/api/documents/*');await p.locator('#save').click();await status(p,'저장 완료');
 });
 await check('guide/results routes and desktop layout',async()=>{
  for(const path of ['/guide/','/results/']){const response=await context.request.get(base+path);assert.equal(response.status(),200);}
  assert.equal(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await mkdir(results,{recursive:true});await p.screenshot({path:join(results,'desktop-1440.png')});
  await b.goto(base+'/guide/');await b.screenshot({path:join(results,'guide-1440.png')});
  await b.locator('.guide section').nth(1).screenshot({path:join(results,'guide-save-diagrams.png')});
 });
 assert.deepEqual(errors,[]);
 await writeFile(join(results,'browser.json'),JSON.stringify({at:new Date().toISOString(),browser:await browser.version(),viewport:{width:1440,height:1000},passed,errors,isolated:true},null,2)+'\n');
} finally {await browser.close();server.kill('SIGTERM');await rm(temp,{recursive:true,force:true});}
