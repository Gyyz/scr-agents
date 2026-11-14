
(function(){
  var state={verdict:'all',sev:'all',cat:'all',q:''};
  var items=[].slice.call(document.querySelectorAll('article.finding'));
  var count=document.getElementById('fcount');
  function apply(){
    var n=0;
    items.forEach(function(a){
      var ok=(state.verdict==='all'||a.dataset.verdict===state.verdict)&&(state.sev==='all'||a.dataset.sev===state.sev)
        &&(state.cat==='all'||a.dataset.cat===state.cat)&&(!state.q||a.dataset.text.indexOf(state.q)>=0);
      a.hidden=!ok; if(ok) n++;
    });
    if(count) count.textContent=n+' of '+items.length+' shown';
  }
  document.querySelectorAll('[data-filter]').forEach(function(b){
    b.addEventListener('click',function(){
      var k=b.dataset.filter; state[k]=b.dataset.value;
      document.querySelectorAll('[data-filter="'+k+'"]').forEach(function(x){x.setAttribute('aria-pressed',String(x===b));});
      apply();
    });
  });
  var sel=document.getElementById('fsev'); if(sel) sel.addEventListener('change',function(){state.sev=sel.value;apply();});
  var cat=document.getElementById('fcat'); if(cat) cat.addEventListener('change',function(){state.cat=cat.value;apply();});
  var q=document.getElementById('fq'); if(q) q.addEventListener('input',function(){state.q=q.value.trim().toLowerCase();apply();});
  var all=document.getElementById('fexpand');
  if(all) all.addEventListener('click',function(){
    var open=all.dataset.open!=='1'; all.dataset.open=open?'1':'0'; all.textContent=open?'Collapse all':'Expand all';
    items.forEach(function(a){if(!a.hidden) a.querySelector('details').open=open;});
  });
  function reveal(){var id=decodeURIComponent(location.hash.slice(1)); var a=id&&document.getElementById(id);
    if(a&&a.classList.contains('finding')){a.hidden=false;a.querySelector('details').open=true;}}
  window.addEventListener('hashchange',reveal); reveal();
  window.addEventListener('beforeprint',function(){document.querySelectorAll('details').forEach(function(d){d.open=true;});});
  apply();
})();
