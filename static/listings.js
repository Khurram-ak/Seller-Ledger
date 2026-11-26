// Listings: every listing per store with eBay traffic (last 30 days) and sales/profit from orders (selected period).
(()=>{
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=(v,c='GBP')=>new Intl.NumberFormat('en-GB',{style:'currency',currency:c,maximumFractionDigits:2}).format(Number(v)||0);
const pct=v=>v===null||v===undefined||!isFinite(v)?'—':`${(v*100).toFixed(1)}%`;
const n=v=>v===null||v===undefined?'—':Number(v).toLocaleString('en-GB');
let last=null,bound=false;

// Status tags, most urgent first. Thresholds are deliberately simple; tune here.
function tags(l){
  const t=[];
  if(l.status==='ENDED')t.push(['ended','Ended']);
  else if(l.quantity_available!==null&&l.quantity_available<=0)t.push(['bad','Out of stock']);
  else if(l.quantity_available!==null&&l.quantity_available<=3)t.push(['warn','Low stock']);
  if(l.profit!==null&&l.profit<0)t.push(['bad','Losing money']);
  else if(l.margin!==null&&l.margin<.10)t.push(['warn','Thin margin']);
  if(l.status!=='ENDED'){
    if(l.views>=50&&!l.transactions)t.push(['warn','Views, no sales']);
    if(l.impressions>=500&&l.ctr!==null&&l.ctr<.01)t.push(['warn','Low click-through']);
    if(l.impressions!==null&&l.impressions<50&&!l.transactions)t.push(['warn','Hardly seen']);
    if(l.missing>0)t.push(['info','Missing cost']);
  }
  if(!t.length&&l.transactions)t.push(['ok','Performing']);
  return t;
}

function build({stores,orders,listings}){
  const ids=new Set(stores.map(s=>s.id));
  const byItem=new Map();
  for(const o of orders){
    const li=o.source_json.lineItems||[];
    for(const l of li){
      const id=String(l.legacyItemId||(l.sku||'').replace(/^ITEM-/,''));
      const key=`${o.account_id}|${id}`,x=byItem.get(key)||{orders:0,units:0,sales:0,profit:0,costed:0,missing:0};
      x.orders++;x.units+=Number(l.quantity||1);
      // Money is per order; only attribute it when the order has a single listing.
      if(li.length===1){x.sales+=Number(o.proceeds||0);if(o.calculation.profit!==null){x.profit+=Number(o.calculation.profit);x.costed+=Number(o.proceeds||0);}else x.missing++;}
      byItem.set(key,x);
    }
  }
  return listings.filter(l=>ids.has(l.account_id)).map(l=>{
    const s=byItem.get(`${l.account_id}|${l.item_id}`)||{orders:0,units:0,sales:0,profit:0,costed:0,missing:0};
    const hasCosted=s.costed>0;
    const r={...l,...s,store:stores.find(x=>x.id===l.account_id),
      profit:hasCosted?s.profit:null,margin:hasCosted?s.profit/s.costed:null,
      conv:l.views?Number(l.transactions||0)/l.views:null};
    r.tags=tags(r);return r;
  });
}

const sorters={profit:r=>r.profit??-1e9,sales:r=>r.sales,views:r=>r.views??-1,impressions:r=>r.impressions??-1,conv:r=>r.conv??-1,margin:r=>r.margin??-1e9,watchers:r=>r.watch_count??-1,stock:r=>-(r.quantity_available??1e9)};

window.renderListings=function(args){
  last=args;const el=$('#listings');if(!el)return;
  const {stores,manager}=args;
  if(!bound){bound=true;['#listing-sort','#listing-show','#listing-search'].forEach(s=>$(s)?.addEventListener('input',()=>last&&window.renderListings(last)));}
  const rows=build(args);
  if(!args.listings.length){el.innerHTML='<p class="muted">No listings synced yet. They load with the next eBay sync (Sync now in eBay connections).</p>';$('#listing-summary').textContent='';return;}
  const show=$('#listing-show').value,q=$('#listing-search').value.trim().toLowerCase(),key=$('#listing-sort').value;
  let list=rows.filter(r=>show==='all'||(show==='active'?r.status==='ACTIVE':show==='attention'?r.tags.some(t=>t[0]==='bad'||t[0]==='warn'):r.status==='ENDED'));
  if(q)list=list.filter(r=>(r.title||'').toLowerCase().includes(q)||r.item_id.includes(q));
  list.sort((a,b)=>sorters[key](b)-sorters[key](a));
  const active=rows.filter(r=>r.status==='ACTIVE'),cur=stores[0]?.currency||'GBP';
  const sum=(xs,f)=>xs.reduce((a,x)=>a+(Number(f(x))||0),0);
  $('#listing-summary').innerHTML=[
    ['Active listings',n(active.length)],
    ['Selling in period',`${n(active.filter(r=>r.orders).length)} <small>of ${n(active.length)}</small>`],
    ['Impressions · 30d',n(sum(active,r=>r.impressions))],
    ['Views · 30d',n(sum(active,r=>r.views))],
    ['Need attention',n(rows.filter(r=>r.tags.some(t=>t[0]==='bad'||t[0]==='warn')).length)],
  ].map(([a,b])=>`<div><span>${a}</span><b>${b}</b></div>`).join('');
  const tag=([k,label])=>`<span class="ins-badge lst-${k}">${label}</span>`;
  el.innerHTML=`<div class="table-wrap lst-wrap"><table><thead><tr><th>Listing</th>${manager&&stores.length>1?'<th>Store</th>':''}<th>Price</th><th>Stock</th><th>Watchers</th><th>Impressions</th><th>Views</th><th>Click-through</th><th>Conversion</th><th>Orders</th><th>Sales</th><th>Profit</th><th>Margin</th><th>Status</th></tr></thead><tbody>${list.slice(0,300).map(r=>`<tr class="${r.status==='ENDED'?'lst-ended':''}">
    <td class="item-cell lst-item">${r.image_url?`<img src="${esc(r.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">`:'<span class="lst-noimg"></span>'}<div><a href="${esc(r.view_url||'#')}" target="_blank" rel="noopener noreferrer"><b title="${esc(r.title)}">${esc(r.title)}</b></a><small class="ins-title">${esc(r.item_id)}${r.has_variations?' · variations':''}${r.quantity_sold?` · ${n(r.quantity_sold)} sold all-time`:''}</small></div></td>
    ${manager&&stores.length>1?`<td>${esc(r.store?.name)}</td>`:''}
    <td>${r.price?(r.has_variations?'from ':'')+money(r.price,r.currency||cur):'—'}</td>
    <td class="${r.quantity_available<=0?'ins-cell-bad':r.quantity_available<=3?'ins-cell-warn':''}">${n(r.quantity_available)}</td>
    <td>${n(r.watch_count)}</td><td>${n(r.impressions)}</td><td>${n(r.views)}</td>
    <td>${r.ctr===null||r.ctr===undefined?'—':pct(r.ctr)}</td><td>${pct(r.conv)}</td>
    <td>${n(r.orders)}</td><td>${money(r.sales,cur)}</td>
    <td class="${r.profit<0?'ins-cell-bad':''}">${r.profit===null?'—':money(r.profit,cur)}</td>
    <td class="${r.margin!==null&&r.margin<0?'ins-cell-bad':r.margin!==null&&r.margin<.1?'ins-cell-warn':''}">${pct(r.margin)}</td>
    <td class="lst-tags">${r.tags.map(tag).join(' ')}</td></tr>`).join('')}</tbody></table></div>
    <p class="footnote">${list.length>300?`Showing 300 of ${list.length}. `:''}Impressions, views, click-through and conversion are eBay figures for the last 30 days. Orders, sales and profit follow the selected date range; profit counts costed orders only. Stock is the quantity still available on eBay.</p>`;
};
})();
