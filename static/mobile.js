// Mobile: label every table cell with its column name so tables can be shown as cards on narrow screens (see demo.css).
(()=>{
const label=()=>document.querySelectorAll('table').forEach(t=>{
  const heads=[...t.querySelectorAll('thead th')].map(th=>th.textContent.trim());
  if(!heads.length)return;
  t.classList.add('cardable');
  t.querySelectorAll('tbody tr').forEach(tr=>[...tr.children].forEach((td,i)=>{if(heads[i]&&td.dataset.label!==heads[i])td.dataset.label=heads[i];}));
});
let queued=false;
new MutationObserver(()=>{if(!queued){queued=true;requestAnimationFrame(()=>{queued=false;label();});}}).observe(document.body,{childList:true,subtree:true});
label();
})();
