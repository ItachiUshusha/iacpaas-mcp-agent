/* A deliberately small Markdown subset. All input becomes text nodes;
   model HTML, scripts, images and links are never executed. */
(function(root){
  function inline(parent, text, doc){
    const pattern=/`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*/g;
    let end=0;
    for(const match of text.matchAll(pattern)){
      parent.append(doc.createTextNode(text.slice(end,match.index)));
      const el=doc.createElement(match[1]!==undefined?'code':match[2]!==undefined?'strong':'em');
      el.textContent=match[1]??match[2]??match[3];parent.append(el);end=match.index+match[0].length;
    }
    parent.append(doc.createTextNode(text.slice(end)));
  }
  function render(parent,text){
    const doc=parent.ownerDocument;parent.replaceChildren();
    const lines=String(text).replace(/\r\n?/g,'\n').split('\n');let paragraph=null,list=null,code=null;
    for(const line of lines){
      if(/^\s*```/.test(line)){
        paragraph=null;list=null;
        if(code){code=null;}else{const pre=doc.createElement('pre');code=doc.createElement('code');pre.append(code);parent.append(pre);}continue;
      }
      if(code){code.append(doc.createTextNode((code.textContent?'\n':'')+line));continue;}
      if(!line.trim()){paragraph=null;list=null;continue;}
      const heading=line.match(/^#{1,6}\s+(.+)$/),item=line.match(/^\s*(?:([-+*])|\d+[.)])\s+(.+)$/);
      if(heading){const h=doc.createElement('h3');inline(h,heading[1],doc);parent.append(h);paragraph=null;list=null;continue;}
      if(item){const tag=item[1]?'ul':'ol';if(!list||list.tagName.toLowerCase()!==tag){list=doc.createElement(tag);parent.append(list);}const li=doc.createElement('li');inline(li,item[2],doc);list.append(li);paragraph=null;continue;}
      list=null;if(!paragraph){paragraph=doc.createElement('p');parent.append(paragraph);}else paragraph.append(doc.createElement('br'));
      inline(paragraph,line,doc);
    }
  }
  root.renderChatMarkdown=render;
})(typeof window==='undefined'?globalThis:window);
