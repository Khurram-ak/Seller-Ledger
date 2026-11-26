// Live eBay: connection management, client access, and live/demo labelling.
(()=>{
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ago=iso=>{if(!iso)return 'never';const m=Math.round((Date.now()-new Date(iso))/6e4);return m<1?'just now':m<60?`${m} min ago`:m<1440?`${Math.round(m/60)} h ago`:`${Math.round(m/1440)} days ago`;};

window.initEbay=function(data){
  const manager=data.user.role==='manager',badge=$('.demo-badge');
  if(data.live){
    if(badge){badge.textContent='LIVE EBAY';badge.classList.add('live-badge');}
    $('#page-subtitle').textContent=manager?'Live data from your connected eBay stores.':'Live data from your assigned eBay store.';
    const synced=data.stores.map(s=>s.sync_at).filter(Boolean).sort().pop();
    if(synced)$('#page-subtitle').textContent+=` Last synced ${ago(synced)}.`;
    const footer=$('footer');if(footer)footer.textContent='Live eBay data · read-only connection · profit is estimated from your entered costs';
  }
  if(!manager){
    if(data.live&&!data.stores.length){const e=$('#error');e.textContent='No eBay store has been assigned to your account yet. Ask your manager to give you access.';e.hidden=false;}
    return;
  }
  const panel=$('#ebay-tools');panel.hidden=false;
  const post=(url,body)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':data.csrf},body:JSON.stringify(body||{})});
  const fail=async r=>{let m='Request failed';try{m=(await r.json()).error||m}catch{}alert(m);};
  const eb=data.ebay||{stores:[]};

  // Connections
  const sheetLine=s=>{if(!s.cost_sheet_report)return '';const r=JSON.parse(s.cost_sheet_report);return `<small>Cost sheet imported ${ago(s.cost_sheet_at)}: ${r.imported} orders costed · ${r.cancelled} cancelled · ${r.unmatched.length} IDs not found${r.unmatched.length?' ('+r.unmatched.slice(0,3).map(esc).join(', ')+')':''} · £${esc(r.extra_charges)} extra eBay charges (ads/labels) from payouts</small>`;};
  $('#ebay-config').innerHTML=eb.configured?`<span class="ins-badge ins-ok">API keys set</span> <span class="muted">${esc(eb.env)} · auto-sync runs in the background</span>`:`<span class="ins-badge ins-bad">Not configured</span> <span class="muted">Add to .env and restart: ${eb.missing.map(esc).join(', ')}</span>`;
  $('#ebay-connect').toggleAttribute('disabled',!eb.configured);
  $('#ebay-stores').innerHTML=eb.stores.length?eb.stores.map(s=>`<div class="compare-row"><div><b>${esc(s.name)}</b><small>${s.ebay_user?'eBay user '+esc(s.ebay_user)+' · ':''}${s.orders} orders · synced ${ago(s.sync_at)}</small>${s.sync_error?`<small class="ins-cell-bad">${esc(s.sync_error)}</small>`:''}${sheetLine(s)}<form class="sheet-form" data-store="${s.id}"><input name="url" value="${esc(s.cost_sheet_url||'')}" placeholder="Cost sheet: Google Sheets link (Anyone with the link can view)"><button class="button">${s.cost_sheet_url?'Re-import':'Link & import'}</button></form></div><div>${s.connected?'<span class="ins-badge ins-ok">Connected</span>':'<span class="ins-badge ins-warn">Disconnected</span>'}</div><div class="ebay-actions">${s.connected?`<button class="button" data-sync="${s.id}">Sync now</button><button class="button" data-disconnect="${s.id}">Disconnect</button>`:'<span class="muted">Reconnect with the button above</span>'}</div></div>`).join(''):'<p class="muted">No eBay stores connected yet. Demo data is shown until you connect one.</p>';
  $('#ebay-stores').addEventListener('click',async e=>{
    const b=e.target.closest('button');if(!b)return;
    if(b.dataset.sync){b.disabled=true;b.textContent='Syncing…';const r=await post(`/api/sync/${b.dataset.sync}`);if(r.ok)location.reload();else{await fail(r);b.disabled=false;b.textContent='Sync now';}}
    if(b.dataset.disconnect&&confirm('Disconnect this store? Synced orders and costs are kept; syncing stops until you reconnect.')){const r=await post(`/api/disconnect/${b.dataset.disconnect}`);if(r.ok)location.reload();else fail(r);}
  });
  $('#ebay-stores').addEventListener('submit',async e=>{
    const f=e.target.closest('.sheet-form');if(!f)return;e.preventDefault();const b=f.querySelector('button');b.disabled=true;b.textContent='Importing…';
    const r=await post(`/api/stores/${f.dataset.store}/cost-sheet`,{url:f.elements.url.value});
    if(r.ok)location.reload();else{await fail(r);b.disabled=false;b.textContent='Link & import';}
  });
  $('#ebay-paste').addEventListener('submit',async e=>{
    e.preventDefault();const btn=e.currentTarget.querySelector('button');btn.disabled=true;btn.textContent='Connecting…';
    const r=await post('/api/oauth/complete',{url:e.currentTarget.elements.url.value});
    if(r.ok)location.reload();else{await fail(r);btn.disabled=false;btn.textContent='Finish connecting';}
  });

  // Product costs: one row per item; unit costs apply per unit to every order of that item.
  const itemMap=new Map();
  for(const o of data.orders)for(const l of o.source_json.lineItems||[]){
    const key=`${o.account_id}|${l.sku}`,it=itemMap.get(key)||{store_id:o.account_id,sku:l.sku,title:l.title,orders:new Set(),units:0,sales:0};
    it.orders.add(o.order_id);it.units+=Number(l.quantity||1);
    if((o.source_json.lineItems||[]).length===1)it.sales+=Number(o.proceeds||0);
    itemMap.set(key,it);
  }
  const def=new Map((data.defaults||[]).map(d=>[`${d.account_id}|${d.sku}`,d]));
  const items=[...itemMap.values()].map(it=>({...it,orders:it.orders.size,cost:def.get(`${it.store_id}|${it.sku}`)})).sort((a,b)=>(!!a.cost-!!b.cost)||b.orders-a.orders);
  const costsEl=$('#item-costs'),storeName=id=>data.stores.find(s=>s.id===id)?.name||'';
  const missingCount=items.filter(i=>!i.cost).length;
  $('#item-costs-summary').textContent=items.length?`${missingCount} of ${items.length} items have no unit cost yet. Items without a cost are listed first.`:'';
  costsEl.innerHTML=items.length?`<div class="table-wrap item-costs-wrap"><table><thead><tr><th>Item</th>${data.stores.length>1?'<th>Store</th>':''}<th>Orders</th><th>Units</th><th>Sales</th><th>Unit cost (£)</th><th>Shipping / unit (£)</th></tr></thead><tbody>${items.map((it,i)=>`<tr data-i="${i}" class="${it.cost?'':'item-missing'}"><td><b>${esc((it.title||'').slice(0,70))}</b><small class="ins-title">${esc(it.sku)}</small></td>${data.stores.length>1?`<td>${esc(storeName(it.store_id))}</td>`:''}<td>${it.orders}</td><td>${it.units}</td><td>£${it.sales.toFixed(2)}</td><td><input class="cost-in" data-kind="product" type="number" min="0" step="0.01" value="${it.cost?esc(it.cost.product_cost):''}" placeholder="required"></td><td><input class="cost-in" data-kind="fulfilment" type="number" min="0" step="0.01" value="${it.cost&&Number(it.cost.fulfilment_cost)?esc(it.cost.fulfilment_cost):''}" placeholder="0.00"></td></tr>`).join('')}</tbody></table></div><div class="item-costs-actions"><button id="save-item-costs" class="primary" disabled>Save costs</button><span id="item-costs-status" class="muted"></span></div>`:'<p class="muted">No items yet. Costs appear here after orders sync.</p>';
  const changed=new Set(),saveBtn=$('#save-item-costs');
  costsEl.addEventListener('input',e=>{if(e.target.classList.contains('cost-in')){changed.add(e.target);saveBtn.disabled=false;}});
  saveBtn&&saveBtn.addEventListener('click',async()=>{
    saveBtn.disabled=true;const status=$('#item-costs-status');let done=0,errors=[];
    // A shipping cost alone can't be saved: the unit cost is what marks an item's cost as known.
    for(const input of changed){
      const it=items[input.closest('tr').dataset.i],v=input.value.trim();
      if(v==='')continue;
      if(input.dataset.kind==='fulfilment'&&!it.cost&&!input.closest('tr').querySelector('[data-kind=product]').value.trim()){errors.push(`${it.sku}: enter a unit cost first`);continue;}
      status.textContent=`Saving ${++done}…`;
      const r=await post('/api/costs',{store_id:it.store_id,sku:it.sku,kind:input.dataset.kind,amount:v,currency:'GBP',note:'Item cost'});
      if(!r.ok){try{errors.push(`${it.sku}: ${(await r.json()).error}`)}catch{errors.push(it.sku)}}
    }
    if(errors.length){alert('Some costs were not saved:\n'+errors.join('\n'));}
    location.reload();
  });

  // Client access
  const clients=data.clients||[],stores=data.stores;
  const access=$('#client-access');
  if(!clients.length){access.innerHTML='<p class="muted">No client accounts exist.</p>';return;}
  if(!stores.length){access.innerHTML='<p class="muted">Connect a store first.</p>';return;}
  access.innerHTML=`<div class="table-wrap"><table><thead><tr><th>Client</th>${stores.map(s=>`<th>${esc(s.name)}</th>`).join('')}</tr></thead><tbody>${clients.map(c=>`<tr><td><b>${esc(c.display_name)}</b><small class="ins-title">${esc(c.username)}</small></td>${stores.map(s=>`<td><input type="checkbox" data-client="${c.id}" data-store="${s.id}" ${c.store_ids.includes(s.id)?'checked':''} aria-label="${esc(c.display_name)} can see ${esc(s.name)}"></td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  access.addEventListener('change',async e=>{
    const x=e.target;if(!x.dataset.client)return;x.disabled=true;
    const r=await post('/api/client-access',{client_id:x.dataset.client,store_id:x.dataset.store,grant:x.checked});
    x.disabled=false;if(!r.ok){x.checked=!x.checked;fail(r);}
  });
};
})();
