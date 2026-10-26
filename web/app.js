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
  const box=document.createElement('section');box.className='facts';box.setAttribute('aria-label','Подтверждённые данные MCP');
  const heading=document.createElement('div');heading.className='facts-heading';heading.textContent='ПОДТВЕРЖДЕНО ИНСТРУМЕНТАМИ';
  const title=document.createElement('div');title.className='facts-title';title.textContent=data.resource.title;
  const metrics=document.createElement('dl');metrics.className='metrics';
  for(const [label,value] of [['Вершин',data.counts.node_occurrences],['Связей',data.counts.edge_occurrences],['Глубина',data.requested_depth]]){
    const group=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');
    dt.textContent=label;dd.textContent=String(value);group.append(dt,dd);metrics.append(group);
  }
  const scope=document.createElement('p');scope.className='scope';
  scope.textContent=(data.source==='saved_snapshot'?'Сохранённый снимок':data.source==='live_api'?'Живой API':data.source)+' · Только полученный фрагмент, не весь граф';
  box.append(heading,title,metrics,scope);el.insertBefore(box,el.querySelector('.content'));
}

function events(el,items){
  const calls=(items||[]).filter(e=>e.event==='tool_call');if(!calls.length)return;
  const log=document.createElement('section');log.className='tool-log';
  const failed=calls.filter(e=>!e.result?.ok).length;
  const heading=document.createElement('p');heading.className='tool-heading';
  heading.textContent=`Вызовы инструментов: ${calls.length-failed} успешно`+(failed?`, ${failed} с ошибкой`:'');log.append(heading);
  const names={list_inforesources:'Поиск ресурса',get_inforesource:'Чтение корня',get_subgraph:'Чтение фрагмента',count_graph:'Подсчёт фрагмента'};
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
    $('#status').style.whiteSpace='pre-line';
  }catch{$('#status').textContent='Сервер недоступен. Проверьте терминал.';}
}

async function send(question){
  if(busy||!question.trim())return;
  busy=true;$('#send').disabled=true;$('#reset').disabled=true;
  message('user',question);$('#question').value='';const answer=message('assistant','Модель обрабатывает запрос…');
  const waitingTop=history.scrollTop;
  let followReply=true,preservedTop=waitingTop;
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Chat-Token':token},body:JSON.stringify({session,question})});
    const data=await r.json();if(data.session)session=data.session;if(data.reset)session='';
    preservedTop=history.scrollTop;followReply=Math.abs(preservedTop-waitingTop)<8;
    renderChatMarkdown(answer.querySelector('.content'),data.answer||data.error||'Не удалось получить ответ.');
    if(!r.ok)answer.classList.add('error');facts(answer,data.events);events(answer,data.events);
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
$('#reset').addEventListener('click',async()=>{
  if(busy)return;
  try{
    if(session){const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Chat-Token':token},body:JSON.stringify({session,question:'reset',reset:true})});if(!r.ok)throw Error();}
    session='';$('#messages').replaceChildren();message('assistant','Новый диалог. Что исследуем?');
  }catch{$('#hint').textContent='Не удалось завершить диалог. Проверьте соединение.';}
});
status();
