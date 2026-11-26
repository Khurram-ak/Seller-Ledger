// Store health insights: weak-spot alerts, store scorecard, top/bottom SKUs.
// Pure client-side over the same /api/state orders the dashboard already loads.
// Profit/margin use complete-cost orders only; missing costs are never treated as zero.
(()=>{
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=(v,c='GBP')=>new Intl.NumberFormat('en-GB',{style:'currency',currency:c,maximumFractionDigits:2}).format(Number(v)||0);
const pct=v=>v===null||!isFinite(v)?'—':`${(v*100).toFixed(1)}%`;
const num=v=>Number(v)||0;

// Thresholds for "needs attention". Tune here.
const T={refundRate:{warn:.05,bad:.10},feeRate:{warn:.15,bad:.20},margin:{warn:.15,bad:.05},missingRate:{warn:.10,bad:.25},growth:{warn:-.10,bad:-.25},idleDays:14,minOrders:3};

function metrics(orders){
  const complete=orders.filter(o=>o.calculation.profit!==null);
  const sales=orders.reduce((a,o)=>a+num(o.proceeds),0);
  const completeSales=complete.reduce((a,o)=>a+num(o.proceeds),0);
  const profit=complete.reduce((a,o)=>a+num(o.calculation.profit),0);
  const refunds=orders.reduce((a,o)=>a+num(o.refunds),0);
  const fees=orders.reduce((a,o)=>a+num(o.fees),0);
  const refunded=orders.filter(o=>num(o.refunds)>0).length;
  return {orders:orders.length,sales,profit,refunds,fees,
    margin:completeSales?profit/completeSales:null,
    refundRate:sales?refunds/sales:null,
    refundedOrders:orders.length?refunded/orders.length:null,
    feeRate:sales?fees/sales:null,
    missingRate:orders.length?(orders.length-complete.length)/orders.length:null,
    missing:orders.length-complete.length};
}

// Sales growth: last 30 days vs the 30 days before, from all of the store's orders.
function growth(allOrders){
  const now=Date.now(),d=864e5;let cur=0,prev=0;
  for(const o of allOrders){const age=(now-new Date(o.created_at))/d;if(age<30)cur+=num(o.proceeds);else if(age<60)prev+=num(o.proceeds);}
  return prev?(cur-prev)/prev:null;
}
function lastOrderDays(allOrders){
  if(!allOrders.length)return null;
  const latest=Math.max(...allOrders.map(o=>+new Date(o.created_at)));
  return Math.floor((Date.now()-latest)/864e5);
}

function level(v,t,lowerIsWorse){
  if(v===null||v===undefined)return null;
  if(lowerIsWorse)return v<t.bad?'bad':v<t.warn?'warn':null;
  return v>t.bad?'bad':v>t.warn?'warn':null;
}

function alertsFor(store,m,g,idle){
  const out=[],add=(lvl,title,detail,fix)=>lvl&&out.push({lvl,store:store.name,title,detail,fix});
  if(m.orders>=T.minOrders){
    if(m.margin!==null&&m.margin<0)add('bad','Losing money',`Margin ${pct(m.margin)} on complete-cost orders (${money(m.profit,store.currency)}).`,'Review product cost and pricing; check fee and fulfilment costs.');
    else add(level(m.margin,T.margin,true),'Thin margin',`Margin ${pct(m.margin)} vs ${pct(T.margin.warn)} target.`,'Raise prices on low-margin SKUs or renegotiate supplier cost.');
    add(level(m.refundRate,T.refundRate),'High refunds',`${pct(m.refundRate)} of sales refunded · ${pct(m.refundedOrders)} of orders.`,'Check listing accuracy, item condition and delivery issues.');
    add(level(m.feeRate,T.feeRate),'High fees',`Fees are ${pct(m.feeRate)} of sales (${money(m.fees,store.currency)}).`,'Review promoted listing rates and category fees.');
  }
  add(level(m.missingRate,T.missingRate),'Missing costs',`${m.missing} of ${m.orders} orders (${pct(m.missingRate)}) have no product cost, so profit is understated in totals.`,'Add order costs so profit is accurate.');
  add(level(g,T.growth,true),'Sales falling',`Sales down ${pct(-g)} in the last 30 days vs the previous 30.`,'Check listing visibility, stock and pricing vs competitors.');
  if(idle!==null&&idle>=T.idleDays)add('warn','No recent orders',`Last order ${idle} days ago.`,'Check that listings are live and in stock.');
  return out;
}

// Score 0–100: margin 40, refunds 20, fees 15, cost completeness 10, growth 15.
function score(m,g){
  const clamp=v=>Math.max(0,Math.min(1,v));
  const parts=[
    [40,m.margin===null?.5:clamp(m.margin/.35)],
    [20,m.refundRate===null?.5:clamp(1-m.refundRate/.15)],
    [15,m.feeRate===null?.5:clamp(1-(m.feeRate-.08)/.17)],
    [10,m.missingRate===null?.5:clamp(1-m.missingRate/.4)],
    [15,g===null?.5:clamp((g+.3)/.6)]];
  return Math.round(parts.reduce((a,[w,v])=>a+w*v,0));
}

function skuRows(stores,orders){
  const map=new Map();
  for(const o of orders){
    const skus=[...new Set((o.source_json?.lineItems||[]).map(l=>l.sku).filter(Boolean))];
    // Only attribute an order to a SKU when it has exactly one; multi-SKU orders can't be split honestly.
    if(skus.length!==1)continue;
    const key=`${o.account_id}|${skus[0]}`;
    const r=map.get(key)||{store:stores.find(s=>s.id===o.account_id),sku:skus[0],title:o.source_json.lineItems[0]?.title,list:[]};
    r.list.push(o);map.set(key,r);
  }
  return [...map.values()].map(r=>({...r,m:metrics(r.list)})).filter(r=>r.store);
}

function badge(lvl){return lvl?`<span class="ins-badge ins-${lvl}">${lvl==='bad'?'Critical':'Watch'}</span>`:'<span class="ins-badge ins-ok">Healthy</span>';}
function cell(v,lvl){return `<td class="${lvl?'ins-cell-'+lvl:''}">${v}</td>`;}

window.renderInsights=function({stores,orders,allOrders,manager}){
  // Profit breakdown: costed orders only, so every line adds up exactly to profit.
  const bd=document.querySelector('#breakdown');
  if(bd){
    const cur=stores[0]?.currency||'GBP';
    const parts=os=>{const c=os.filter(o=>o.calculation.profit!==null),t=k=>c.reduce((a,o)=>a+num(k(o)),0);
      return {n:c.length,missing:os.length-c.length,missingSales:os.filter(o=>o.calculation.profit===null).reduce((a,o)=>a+num(o.proceeds),0),
        est:c.filter(o=>o.calculation.estimated).length,sales:t(o=>o.proceeds),fees:t(o=>o.fees),refunds:t(o=>o.refunds),item:t(o=>o.calculation.product),ship:t(o=>o.calculation.fulfilment),other:t(o=>o.calculation.other),profit:t(o=>o.calculation.profit),share:t(o=>o.calculation.my_share)};};
    const all=parts(orders),of=v=>all.sales?` <small>${pct(v/all.sales)}</small>`:'';
    const lines=[['Sales of costed orders','sales',all.sales,all.missing?`${all.n} of ${all.n+all.missing} orders · excl. VAT`:'Item price + postage charged, excl. VAT'],['eBay fees','neg',all.fees,'Final value + per-order fees'],['Refunds','neg',all.refunds,'Money returned to buyers'],['Item cost','neg',all.item,'AliExpress / supplier cost'],['Shipping & prep','neg',all.ship,'From your cost sheet'],['Other eBay charges','neg',all.other,'Ads, labels and fees not in eBay order data']];
    const max=Math.max(1,all.sales);
    const estNote=all.est?`<p class="bd-note bd-est"><b>${all.est} of ${all.n} orders use estimated item costs</b>, taken from each item's last order in your cost sheet. They switch to the real cost when the sheet is updated. Estimates don't include ad or label charges.</p>`:'';
    const note=all.missing?`<p class="bd-note">Only <b>${all.n} of ${all.n+all.missing} orders</b> (${money(all.sales,cur)} of ${money(all.sales+all.missingSales,cur)} sales) have an item cost yet, so profit below covers those orders only. The other ${all.missing} are items never seen in the cost sheet; they appear once added.</p>`:'';
    bd.innerHTML=all.n?`${note}${estNote}<div class="bd-rows">${lines.map(([label,cls,v,hint])=>`<div class="bd-row"><div><b>${label}</b><small>${hint}</small></div><div class="bd-bar"><i class="bd-${cls}" style="width:${Math.max(.5,v/max*100)}%"></i></div><div class="bd-val ${cls==='neg'?'bd-neg-text':''}">${cls==='neg'?'−':''}${money(v,cur)}${of(v)}</div></div>`).join('')}
      <div class="bd-row bd-total"><div><b>Profit</b><small>${all.n} costed orders${all.est?` (${all.est} estimated)`:''}</small></div><div class="bd-bar"><i class="bd-${all.profit<0?'loss':'profit'}" style="width:${Math.max(.5,Math.abs(all.profit)/max*100)}%"></i></div><div class="bd-val">${money(all.profit,cur)}${of(all.profit)}</div></div>
      <div class="bd-row bd-total"><div><b>Your share</b><small>${manager?'Each store’s own %':'Your % of profit'}</small></div><div class="bd-bar"><i class="bd-share" style="width:${Math.max(.5,Math.abs(all.share)/max*100)}%"></i></div><div class="bd-val">${money(all.share,cur)}</div></div></div>
      ${all.missing?`<p class="footnote">Not included: ${all.missing} orders (${money(all.missingSales,cur)} sales) without an item cost yet. Add them to the cost sheet or the Product costs table.</p>`:''}
      ${stores.length>1?`<div class="table-wrap"><table><thead><tr><th>Store</th><th>Share</th><th>Costed orders</th><th>Sales</th><th>eBay fees</th><th>Refunds</th><th>Item cost</th><th>Shipping & prep</th><th>Other eBay</th><th>Profit</th><th>Your share</th></tr></thead><tbody>${stores.map(st=>{const p=parts(orders.filter(o=>o.account_id===st.id));return `<tr><td><b>${esc(st.name)}</b></td><td>${esc(st.share)}%</td><td>${p.n}${p.missing?` <small class="ins-cell-warn">+${p.missing} missing</small>`:''}</td><td>${money(p.sales,cur)}</td><td>${money(p.fees,cur)}</td><td>${money(p.refunds,cur)}</td><td>${money(p.item,cur)}</td><td>${money(p.ship,cur)}</td><td>${money(p.other,cur)}</td><td class="${p.profit<0?'ins-cell-bad':''}"><b>${money(p.profit,cur)}</b></td><td><b>${money(p.share,cur)}</b></td></tr>`;}).join('')}</tbody></table></div>`:''}`
      :`<p class="muted">None of the ${all.missing} orders in this period (${money(all.missingSales,cur)} sales) have an item cost yet, so profit can't be worked out. They appear once they're added to the cost sheet.</p>`;
  }

  const alertsEl=document.querySelector('#alerts'),scoreEl=document.querySelector('#scorecard'),skuEl=document.querySelector('#skus');
  if(!alertsEl)return;
  const per=stores.map(s=>{
    const os=orders.filter(o=>o.account_id===s.id),all=allOrders.filter(o=>o.account_id===s.id);
    const m=metrics(os),g=growth(all),idle=lastOrderDays(all);
    return {s,m,g,idle,alerts:alertsFor(s,m,g,idle),score:score(m,g)};
  });

  // Alerts
  const alerts=per.flatMap(p=>p.alerts).sort((a,b)=>(a.lvl==='bad'?0:1)-(b.lvl==='bad'?0:1));
  document.querySelector('#alert-count').textContent=alerts.length?`${alerts.filter(a=>a.lvl==='bad').length} critical · ${alerts.filter(a=>a.lvl==='warn').length} watch`:'All clear';
  alertsEl.innerHTML=alerts.length?alerts.map(a=>`<div class="ins-alert ins-alert-${a.lvl}"><div>${badge(a.lvl)} <b>${esc(a.title)}</b>${manager?` <span class="muted">· ${esc(a.store)}</span>`:''}<p>${esc(a.detail)}</p><small>→ ${esc(a.fix)}</small></div></div>`).join(''):'<p class="muted">No weak spots detected in this period. Thresholds: margin under 15%, refunds over 5% of sales, fees over 15%, missing costs over 10%, sales down over 10%.</p>';

  // Scorecard
  const ranked=[...per].sort((a,b)=>b.score-a.score);
  scoreEl.innerHTML=ranked.length?`<table><thead><tr><th>#</th><th>Store</th><th>Health</th><th>Orders</th><th>Sales</th><th>Est. profit</th><th>Margin</th><th>Refund rate</th><th>Fee rate</th><th>Missing costs</th><th>30-day growth</th><th>Verdict</th></tr></thead><tbody>${ranked.map((p,i)=>{
    const worst=p.alerts.some(a=>a.lvl==='bad')?'bad':p.alerts.length?'warn':null;
    const verdict=worst==='bad'?'Fix first':worst?'Watch':p.g!==null&&p.g>0?'Scale':'Steady';
    return `<tr><td>${i+1}</td><td><b>${esc(p.s.name)}</b></td><td><div class="ins-score"><i style="width:${p.score}%" class="ins-score-${p.score>=70?'ok':p.score>=45?'warn':'bad'}"></i></div><small>${p.score}/100</small></td><td>${p.m.orders}</td><td>${money(p.m.sales,p.s.currency)}</td><td>${money(p.m.profit,p.s.currency)}</td>${cell(pct(p.m.margin),level(p.m.margin,T.margin,true)||(p.m.margin<0?'bad':null))}${cell(pct(p.m.refundRate),level(p.m.refundRate,T.refundRate))}${cell(pct(p.m.feeRate),level(p.m.feeRate,T.feeRate))}${cell(pct(p.m.missingRate),level(p.m.missingRate,T.missingRate))}${cell(p.g===null?'—':(p.g>=0?'+':'')+pct(p.g),level(p.g,T.growth,true))}<td><span class="ins-badge ins-${worst||'ok'}">${verdict}</span></td></tr>`;}).join('')}</tbody></table>`:'<p class="muted">No stores in this view.</p>';

  // SKUs
  const rows=skuRows(stores,orders).filter(r=>r.m.orders-r.m.missing>0);
  const byProfit=[...rows].sort((a,b)=>b.m.profit-a.m.profit);
  const top=byProfit.slice(0,5),bottom=byProfit.slice(-5).reverse().filter(r=>!top.includes(r));
  const table=(list,label)=>`<h3 class="ins-sub">${label}</h3><table><thead><tr><th>Item</th>${manager?'<th>Store</th>':''}<th>Orders</th><th>Sales</th><th>Est. profit</th><th>Margin</th><th>Refunded orders</th><th>Missing costs</th></tr></thead><tbody>${list.map(r=>`<tr><td class="item-cell"><b title="${esc(r.title||'')}">${esc(r.title||r.sku)}</b><small class="ins-title">${esc(r.sku)}</small></td>${manager?`<td>${esc(r.store.name)}</td>`:''}<td>${r.m.orders}</td><td>${money(r.m.sales,r.store.currency)}</td><td>${money(r.m.profit,r.store.currency)}</td>${cell(pct(r.m.margin),r.m.margin<0?'bad':level(r.m.margin,T.margin,true))}${cell(pct(r.m.refundedOrders),level(r.m.refundedOrders,{warn:.10,bad:.20}))}<td>${r.m.missing}</td></tr>`).join('')}</tbody></table>`;
  skuEl.innerHTML=rows.length?`<div class="ins-sku-grid"><div class="table-wrap">${table(top,'Top items: scale these')}</div><div class="table-wrap">${bottom.length?table(bottom,'Bottom items: fix or drop'):'<p class="muted">Not enough items to compare.</p>'}</div></div><p class="footnote">Profit counts complete-cost orders only. Orders with more than one SKU aren't split between SKUs, so they're left out of this table.</p>`:'<p class="muted">No SKU data with complete costs in this period.</p>';
};
})();
