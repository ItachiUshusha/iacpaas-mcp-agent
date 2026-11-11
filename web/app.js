const $=s=>document.querySelector(s);
let session='',token='',busy=false;
const history=$('#messages');
history.tabIndex=0;history.setAttribute('aria-label','История диалога');
const jump=document.createElement('button');jump.type='button';jump.className='jump-bottom';
jump.textContent='В конец ↓';jump.setAttribute('aria-label','Прокрутить к последнему сообщению');jump.hidden=true;
$('main').insertBefore(jump,$('footer'));
function updateJump(){jump.hidden=history.scrollHeight-history.scrollTop-history.clientHeight<70;}
function scrollToMessage(el){
  history.scrollTop+=el.getBoundingClientRect().top-history.getBoundingClientRect().top-16;
  updateJump();
}
jump.addEventListener('click',()=>{history.scrollTop=history.scrollHeight;updateJump();});
history.addEventListener('scroll',updateJump,{passive:true});
window.addEventListener('resize',updateJump);

function message(role,text){
  $('.welcome')?.remove();
  const el=document.createElement('article');el.className='message '+role;
  const label=document.createElement('strong');label.className='speaker';label.textContent=role==='user'?'ВЫ':'АГЕНТ';
  const content=document.createElement('div');content.className='content';content.textContent=text;
  el.append(label,content);history.append(el);history.scrollTop=history.scrollHeight;updateJump();return el;
}

function facts(el,items){
  const matches=(items||[]).filter(e=>e.event==='tool_call'&&e.result?.ok&&e.result.data?.counts&&e.result.data?.resource);
  const data=matches.at(-1)?.result.data;
  if(!data)return;
  const box=document.createElement('section');box.className='facts';box.setAttribute('aria-label','Статистика прочитанного фрагмента');
  const heading=document.createElement('div');heading.className='facts-heading';heading.textContent='ПРОЧИТАННЫЙ ФРАГМЕНТ';
  const title=document.createElement('div');title.className='facts-title';title.textContent=data.resource.title;
  const metrics=document.createElement('dl');metrics.className='metrics';
  for(const [label,value] of [['Вершин',data.counts.node_occurrences],['Связей',data.counts.edge_occurrences],['Глубина',data.requested_depth]]){
    const group=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');
    dt.textContent=label;dd.textContent=String(value);group.append(dt,dd);metrics.append(group);
  }
  const scope=document.createElement('p');scope.className='scope';
  scope.textContent=(data.source==='saved_snapshot'?'Сохранённый снимок':data.source==='live_api'?'Живой API':data.source)+' · Статистика чтения, не подтверждение вывода модели';
  box.append(heading,title,metrics,scope);el.append(box);
}

function events(el,items){
  const calls=(items||[]).filter(e=>e.event==='tool_call');if(!calls.length)return;
  const log=document.createElement('section');log.className='tool-log';
  const failed=calls.filter(e=>!e.result?.ok).length;
  const heading=document.createElement('p');heading.className='tool-heading';
  heading.textContent=`Вызовы инструментов: ${calls.length-failed} успешно`+(failed?`, ${failed} с ошибкой`:'');log.append(heading);
  const names={list_inforesources:'Поиск ресурса',get_inforesource:'Чтение корня',get_subgraph:'Чтение фрагмента',count_graph:'Подсчёт фрагмента',create_folder:'Создание папки',delete_folder:'Удаление папки в корзину',search_inforesource:'Поиск внутри ресурса',compare_node_children:'Пересечение двух списков'};
  for(const item of calls){
    const d=document.createElement('details'),s=document.createElement('summary'),p=document.createElement('pre');
    d.className=item.result?.ok?'tool-ok':'tool-error';
    s.textContent=(item.result?.ok?'✓ ':'! ')+(names[item.name]||item.name)+(item.result?.ok?' — выполнено':' — запрос отклонён');
    d.append(s);
    if(!item.result?.ok){const hint=document.createElement('p');hint.textContent=[item.result?.error?.message,item.result?.error?.hint].filter(Boolean).join(' ');d.append(hint);}
    p.textContent=JSON.stringify({tool:item.name,arguments:item.arguments,result:item.result},null,2);d.append(p);log.append(d);
  }
  el.append(log);
}

async function status(){
  try{
    const r=await fetch('/api/status');if(!r.ok)throw Error();const s=await r.json();token=s.token;
    $('#status').textContent=`${s.model}\n${s.source==='snapshot'?'Сохранённый корень':s.source==='live'?'Живой API':'Режим MCP-сервера'}\n${s.model_configured?'Параметры модели заданы':'Ожидается API-ключ в терминале'}`;
    if(s.generation?.deterministic)$('#status').textContent+='\nВоспроизводимый режим · seed '+s.generation.seed;
    $('#status').style.whiteSpace='pre-line';
  }catch{$('#status').textContent='Сервер недоступен. Проверьте терминал.';}
}

async function send(question,action=''){
  if(busy||!question.trim())return;
  busy=true;$('#send').disabled=true;$('#reset').disabled=true;
  message('user',question);$('#question').value='';
  const answer=message('assistant',action==='list_materials'?'Читаю каталог MCP…':'Модель обрабатывает запрос…');
  const waitingTop=history.scrollTop;
  let followReply=true,preservedTop=waitingTop;
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Chat-Token':token},body:JSON.stringify({session,question,action})});
    const data=await r.json();if(data.session)session=data.session;if(data.reset)session='';
    preservedTop=history.scrollTop;followReply=Math.abs(preservedTop-waitingTop)<8;
    renderChatMarkdown(answer.querySelector('.content'),data.answer||data.error||'Не удалось получить ответ.');
    if(!r.ok)answer.classList.add('error');
    grounding(answer,data.grounding,data.events);
    comparison(answer,data.events);facts(answer,data.events);events(answer,data.events);
  }catch{
    answer.querySelector('.content').textContent='Соединение прервано. Ответ не подтверждён; начните новый диалог перед повтором.';answer.classList.add('error');
  }finally{
    busy=false;$('#send').disabled=false;$('#reset').disabled=false;
    if(followReply)scrollToMessage(answer);else history.scrollTop=preservedTop;
    updateJump();
  }
}

$('#form').addEventListener('submit',e=>{e.preventDefault();send($('#question').value);});
$('#question').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send($('#question').value);}});
document.querySelectorAll('[data-question]').forEach(b=>b.addEventListener('click',()=>send(b.dataset.question)));
document.querySelectorAll('[data-catalog]').forEach(button=>button.addEventListener('click',()=>
  send('Какие ресурсы о материалах есть в настроенном каталоге?','list_materials')));
$('#reset').addEventListener('click',async()=>{
  if(busy)return;
  try{
    if(session){const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Chat-Token':token},body:JSON.stringify({session,question:'reset',reset:true})});if(!r.ok)throw Error();}
    session='';$('#messages').replaceChildren();message('assistant','Новый диалог. Что исследуем?');
  }catch{$('#hint').textContent='Не удалось завершить диалог. Проверьте соединение.';}
});
status();

function grounding(el,audit,items){
  if(!audit)return;
  const calls=(items||[]).filter(e=>e.event==='tool_call');
  const folderOperations={create_folder:'created',delete_folder:'moved_to_trash'};
  const onlyConfirmedFolderChanges=calls.length>0&&calls.every(e=>
    e.result?.ok&&folderOperations[e.name]&&e.result.data?.operation===folderOperations[e.name]);
  if(audit.status==='uncited'&&onlyConfirmedFolderChanges)return;
  const panel=document.createElement('section');panel.className='grounding '+audit.status;
  const title=document.createElement('strong');
  title.textContent=audit.status==='cited'?'ССЫЛКИ НА ЗАПИСИ ПРОВЕРЕНЫ':'ВЫВОД НЕ ПОДТВЕРЖДЁН ССЫЛКАМИ';
  const note=document.createElement('p');note.textContent=audit.note;panel.append(title,note);
  const targets=new Map();
  for(const source of audit.sources||[]){
    const d=document.createElement('details'),summary=document.createElement('summary');
    summary.textContent='['+source.evidence_id+'] '+source.trail.join(' → ');
    const provenance=document.createElement('p');
    provenance.textContent=source.resource.title+' · '+source.resource.path+' · '+
      (source.source==='live_api'?'Живой API':'Сохранённый снимок')+' · Глубина '+source.requested_depth;
    const locator=document.createElement('p');locator.textContent='Указатель в экспорте: '+(source.pointer||'(корень)');
    const raw=document.createElement('pre');raw.textContent=JSON.stringify({fields:source.fields,children:source.children,children_truncated:source.children_truncated},null,2);
    d.append(summary,provenance,locator,raw);panel.append(d);targets.set(source.evidence_id,d);
  }
  el.append(panel);
  // Turn existing evidence markers into local controls, never into model-supplied URLs.
  const walker=document.createTreeWalker(el.querySelector('.content'),NodeFilter.SHOW_TEXT);
  const nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);
  for(const text of nodes){
    if(text.parentElement.closest('pre,code,a,button'))continue;
    const pattern=/\[(E[A-Za-z0-9]+)\]/g;
    let found,offset=0;const fragment=document.createDocumentFragment();
    while((found=pattern.exec(text.nodeValue))){
      fragment.append(document.createTextNode(text.nodeValue.slice(offset,found.index)));
      const target=targets.get(found[1]);
      if(target){
        const button=document.createElement('button');button.type='button';button.className='evidence-link';
        button.textContent=found[0];button.title='Показать исходную запись';
        button.onclick=()=>{target.open=true;target.scrollIntoView({block:'nearest',behavior:'smooth'});};
        fragment.append(button);
      }else fragment.append(document.createTextNode(found[0]));
      offset=pattern.lastIndex;
    }
    if(offset){fragment.append(document.createTextNode(text.nodeValue.slice(offset)));text.replaceWith(fragment);}
  }
}

function comparison(el,items){
  const found=(items||[]).filter(e=>e.event==='tool_call'&&e.name==='compare_node_children'&&e.result?.ok).at(-1);
  if(!found)return;
  const data=found.result.data,panel=document.createElement('section');panel.className='comparison';
  const title=document.createElement('strong');title.textContent='ПЕРЕСЕЧЕНИЕ СПИСКОВ — РЕЗУЛЬТАТ КОДА';
  const paths=document.createElement('p');paths.textContent=data.left.trail.join(' → ')+' / '+data.right.trail.join(' → ');
  const list=document.createElement('ul');
  for(const item of data.common){
    const li=document.createElement('li');li.textContent=item.label+' — слева: '+
      item.left_evidence.map(e=>e.evidence_id).join(', ')+', справа: '+item.right_evidence.map(e=>e.evidence_id).join(', ');
    list.append(li);
  }
  const scope=document.createElement('p');scope.textContent=data.rule+' Результат относится только к выбранным спискам.';
  if(!data.common.length){const empty=document.createElement('p');empty.textContent='В этих двух списках совпадающих меток не найдено.';list.append(empty);}
  panel.append(title,paths,list,scope);el.append(panel);
}
