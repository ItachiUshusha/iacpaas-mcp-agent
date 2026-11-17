/* A small Markdown subset rendered with DOM elements and text nodes;
   model HTML, scripts, images and links are never executed. */
(function(root){
  function inline(parent, text, doc){
    const pattern=/`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|<br\s*\/?>/gi;
    let end=0;
    for(const match of text.matchAll(pattern)){
      parent.append(doc.createTextNode(text.slice(end,match.index)));
      if(match[1]===undefined&&match[2]===undefined&&match[3]===undefined){
        parent.append(doc.createElement('br'));end=match.index+match[0].length;continue;
      }
      const el=doc.createElement(match[1]!==undefined?'code':match[2]!==undefined?'strong':'em');
      el.textContent=match[1]??match[2]??match[3];parent.append(el);end=match.index+match[0].length;
    }
    parent.append(doc.createTextNode(text.slice(end)));
  }
  function tableCells(line){
    const text=line.trim().replace(/^\|/,'').replace(/(?<!\\)\|$/,'');
    const cells=text.split(/(?<!\\)\|/);
    return cells.length>1?cells.map(cell=>cell.trim().replace(/\\\|/g,'|')):null;
  }

  function renderTable(parent, lines, index, doc){
    const headers=tableCells(lines[index]);
    const separators=tableCells(lines[index+1]||'');
    if(!headers||separators?.length!==headers.length||!separators.every(cell=>/^:?-{3,}:?$/.test(cell)))return index;
    const wrapper=doc.createElement('div');
    wrapper.className='chat-table';
    const table=doc.createElement('table'),head=doc.createElement('thead'),body=doc.createElement('tbody');
    function appendRow(section,cells,tag){
      const row=doc.createElement('tr');
      for(const text of cells){
        const cell=doc.createElement(tag);
        if(tag==='th')cell.setAttribute('scope','col');
        inline(cell,text,doc);
        row.append(cell);
      }
      section.append(row);
    }
    appendRow(head,headers,'th');
    let end=index+2;
    while(end<lines.length){
      const cells=tableCells(lines[end]);
      if(cells?.length!==headers.length)break;
      appendRow(body,cells,'td');
      end++;
    }
    table.append(head,body);
    wrapper.append(table);
    parent.append(wrapper);
    return end-1;
  }

  function render(parent,text){
    const doc=parent.ownerDocument;parent.replaceChildren();
    const lines=String(text).replace(/\r\n?/g,'\n').split('\n');let paragraph=null,list=null,code=null;
    for(let i=0;i<lines.length;i++){
      const line=lines[i];
      if(/^\s*```/.test(line)){
        paragraph=null;list=null;
        if(code){code=null;}else{const pre=doc.createElement('pre');code=doc.createElement('code');pre.append(code);parent.append(pre);}continue;
      }
      if(code){code.append(doc.createTextNode((code.textContent?'\n':'')+line));continue;}
      if(!line.trim()){paragraph=null;list=null;continue;}
      const tableEnd=renderTable(parent,lines,i,doc);
      if(tableEnd!==i){i=tableEnd;paragraph=null;list=null;continue;}
      const heading=line.match(/^#{1,6}\s+(.+)$/),item=line.match(/^\s*(?:([-+*])|\d+[.)])\s+(.+)$/);
      if(heading){const h=doc.createElement('h3');inline(h,heading[1],doc);parent.append(h);paragraph=null;list=null;continue;}
      if(item){const tag=item[1]?'ul':'ol';if(!list||list.tagName.toLowerCase()!==tag){list=doc.createElement(tag);parent.append(list);}const li=doc.createElement('li');inline(li,item[2],doc);list.append(li);paragraph=null;continue;}
      list=null;if(!paragraph){paragraph=doc.createElement('p');parent.append(paragraph);}else paragraph.append(doc.createElement('br'));
      inline(paragraph,line,doc);
    }
  }
  root.renderChatMarkdown=render;
})(typeof window==='undefined'?globalThis:window);
