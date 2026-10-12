//
// app/api/docs_static/docs-nav.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Sidebar and light/dark theme toggle for the Swagger UI page. Served self-hosted under
// /docs-static so the page keeps its default-src 'self' CSP without an inline-script exception.
// The navigation is built from the loaded OpenAPI document (the page's global `ui`), not from the
// DOM: Swagger UI renders collapsed operations lazily, so the DOM does not list them all.
// Icons are inline Material Icons paths (Apache-2.0).
(function(){
 var ICONS={
  auto:"M10.85 12.65h2.3L12 9l-1.15 3.65zM20 8.69V4h-4.69L12 .69 8.69 4H4v4.69L.69 12 4 15.31V20h4.69L12 23.31 15.31 20H20v-4.69L23.31 12 20 8.69zM14.3 16l-.7-2h-3.2l-.7 2H7.8L11 7h2l3.2 9h-1.9z",
  light:"M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zM2 13h2c.55 0 1-.45 1-1s-.45-1-1-1H2c-.55 0-1 .45-1 1s.45 1 1 1zm18 0h2c.55 0 1-.45 1-1s-.45-1-1-1h-2c-.55 0-1 .45-1 1s.45 1 1 1zM11 2v2c0 .55.45 1 1 1s1-.45 1-1V2c0-.55-.45-1-1-1s-1 .45-1 1zm0 18v2c0 .55.45 1 1 1s1-.45 1-1v-2c0-.55-.45-1-1-1s-1 .45-1 1zM5.99 4.58c-.39-.39-1.03-.39-1.41 0-.39.39-.39 1.03 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0s.39-1.03 0-1.41L5.99 4.58zm12.37 12.37c-.39-.39-1.03-.39-1.41 0-.39.39-.39 1.03 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0 .39-.39.39-1.03 0-1.41l-1.06-1.06zm1.06-10.96c.39-.39.39-1.03 0-1.41-.39-.39-1.03-.39-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06zM7.05 18.36c.39-.39.39-1.03 0-1.41-.39-.39-1.03-.39-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06z",
  dark:"M12 3c-4.97 0-9 4.03-9 9s4.03 9 9 9 9-4.03 9-9c0-.46-.04-.92-.1-1.36-.98 1.37-2.58 2.26-4.4 2.26-2.98 0-5.4-2.42-5.4-5.4 0-1.81.89-3.42 2.26-4.4-.44-.06-.9-.1-1.36-.1z"
 };
 var MODES=['auto','light','dark'],
  LABEL={auto:'Theme: automatic (system)',light:'Theme: light',dark:'Theme: dark'};
 var media=window.matchMedia('(prefers-color-scheme: dark)'),mode='auto';
 try{mode=localStorage.getItem('docs-theme')||'auto'}catch(e){}
 if(MODES.indexOf(mode)<0){mode='auto'}
 var btn=document.createElement('button');btn.id='theme-toggle';btn.type='button';
 var svgNS='http://www.w3.org/2000/svg',svg=document.createElementNS(svgNS,'svg'),path=document.createElementNS(svgNS,'path');
 svg.setAttribute('viewBox','0 0 24 24');svg.appendChild(path);btn.appendChild(svg);
 function apply(){
  var dark=mode==='dark'||(mode==='auto'&&media.matches);
  document.documentElement.classList.toggle('dark',dark);
  path.setAttribute('d',ICONS[mode]);btn.title=LABEL[mode]+' \u2014 click to switch';
  btn.setAttribute('aria-label',btn.title);
 }
 btn.addEventListener('click',function(){
  mode=MODES[(MODES.indexOf(mode)+1)%MODES.length];
  try{localStorage.setItem('docs-theme',mode)}catch(e){}
  apply();
 });
 media.addEventListener('change',apply);
 apply();document.body.appendChild(btn);
})();
(function(){
 var METHODS=['get','put','post','delete','patch','options','head'];
 function el(tag,cls,text){
  var e=document.createElement(tag);if(cls){e.className=cls}if(text){e.textContent=text}return e;
 }
 function slug(s){return String(s).trim().replace(/\s/g,'_')}
 function open(tag,opId,a){
  ui.layoutActions.show(['operations-tag',tag],true);
  ui.layoutActions.show(['operations',tag,opId],true);
  document.querySelectorAll('#nav-side a.on').forEach(function(x){x.classList.remove('on')});
  a.classList.add('on');
  // scroll only once the operation is expanded: an earlier smooth scroll is cut off by the re-render
  var tries=0;(function go(){
   var t=document.getElementById('operations-'+slug(tag)+'-'+slug(opId));
   if(!(t&&t.classList.contains('is-open')&&t.querySelector('.opblock-body'))){
    if(++tries<40){setTimeout(go,50)}return;
   }
   requestAnimationFrame(function(){
    window.scrollTo({top:t.getBoundingClientRect().top+window.scrollY-12,behavior:'smooth'});
    history.replaceState(null,'','#/'+slug(tag)+'/'+slug(opId));
   });
  })();
 }
 function build(){
  if(typeof ui==='undefined'||!ui.specSelectors){return false}
  var spec=ui.specSelectors.specJson().toJS();
  if(!spec.paths){return false}
  var groups={};
  Object.keys(spec.paths).sort().forEach(function(path){
   METHODS.forEach(function(m){
    var op=spec.paths[path][m];if(!op){return}
    var tag=(op.tags&&op.tags[0])||'default';
    (groups[tag]=groups[tag]||[]).push({m:m,path:path,op:op,tag:tag});
   });
  });
  var nav=el('nav');nav.id='nav-side';
  var hd=el('div','hd');hd.appendChild(el('div','ti',(spec.info&&spec.info.title)||'API'));
  var q=el('input');q.type='search';q.placeholder='Filter: path, method, description';
  q.setAttribute('aria-label','Filter API endpoints');hd.appendChild(q);
  nav.appendChild(hd);
  Object.keys(groups).sort().forEach(function(tag){
   var d=el('details');d.open=true;d.appendChild(el('summary','',tag));
   groups[tag].forEach(function(e){
    var a=el('a');a.href='#';
    a.appendChild(el('span','m '+e.m,e.m.toUpperCase()));var p=el('span','p');
    e.path.split('/').forEach(function(part,i){  // allow line breaks only after a slash
     if(i){p.appendChild(document.createTextNode('/'));p.appendChild(el('wbr'))}
     p.appendChild(document.createTextNode(part));
    });
    a.appendChild(p);
    if(e.op.summary){a.appendChild(el('span','s',e.op.summary))}
    a.title=e.m.toUpperCase()+' '+e.path+(e.op.summary?' \u2014 '+e.op.summary:'');
    a.dataset.k=(tag+' '+e.m+' '+e.path+' '+(e.op.summary||'')).toLowerCase();
    a.addEventListener('click',function(ev){ev.preventDefault();open(tag,e.op.operationId,a)});
    d.appendChild(a);
   });
   nav.appendChild(d);
  });
  q.addEventListener('input',function(){
   var t=q.value.toLowerCase().trim();
   nav.querySelectorAll('details').forEach(function(d){
    var any=false;
    d.querySelectorAll('a').forEach(function(a){
     var hit=a.dataset.k.indexOf(t)>=0;a.style.display=hit?'':'none';any=any||hit;
    });
    d.style.display=any?'':'none';if(t){d.open=true}
   });
  });
  document.body.appendChild(nav);document.body.classList.add('nav-on');
  return true;
 }
 var tries=0,t=setInterval(function(){if(build()||++tries>150){clearInterval(t)}},200);
})();
