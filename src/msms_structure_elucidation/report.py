"""Self-contained offline HTML mirror spectrum report."""
from __future__ import annotations

import html
import json
from pathlib import Path


def write_report(result: dict, target: Path) -> Path:
    payload = json.dumps(result, ensure_ascii=True).replace('</', '<\\/')
    title = html.escape(Path(result['input']).name)
    target.write_text('''<!doctype html><html lang="en"><meta charset="utf-8">
<title>MS/MS structure elucidation</title>
<style>body{font:15px system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#17232f;background:#f6f8fa}
h1{font-size:1.7rem}.note{background:#fff4dc;padding:1rem;border-radius:8px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}
.card{background:white;border:1px solid #dce2e7;padding:1rem;border-radius:9px;cursor:pointer;text-align:left;color:inherit}
.card.active{outline:2px solid #1769aa}code{overflow-wrap:anywhere}.metric{font-weight:700}.plot{background:white;border:1px solid #dce2e7;border-radius:9px;padding:1rem;margin-top:1rem}
.card img{display:block;width:100%;height:170px;object-fit:contain}
svg{width:100%;height:440px}.axis{stroke:#667}.exp{stroke:#175eab}.pred{stroke:#db7036}.matched{stroke:#2b995b}.labels{display:flex;gap:18px}
</style><h1>MS/MS structure elucidation: '''+title+'''</h1><div id="summary"></div><div class="grid" id="cards"></div>
<div class="plot"><label>Collision energy <select id="ce"></select></label> <button id="save-svg">Download SVG</button> <button id="save-png">Download PNG</button><div class="labels"><span style="color:#175eab">Experimental ↑</span><span style="color:#db7036">Predicted ↓</span><span style="color:#2b995b">Matched</span></div><svg id="mirror" viewBox="0 0 1000 440" role="img" aria-label="Mirror mass spectrum"></svg><div id="peak-note"></div></div>
<section id="denovo"></section>
<script id="data" type="application/json">'''+payload+'''</script>
<script>
const data=JSON.parse(document.getElementById('data').textContent), cards=document.getElementById('cards'), ce=document.getElementById('ce');
const candidates=data.candidates||[];let selected=0;
function node(tag,text){const e=document.createElement(tag);e.textContent=text;return e}
const summary=document.getElementById('summary');summary.append(node('p',`Precursor ${data.parentmass} · ${data.adduct} · ${data.peaks} peaks · Formula ${data.formula||'unknown'} (${data.formula_source||'unavailable'}) · Experimental energies: ${data.collision_unit||'unspecified'}`));
if(data.warnings?.length){const warning=node('div',data.warnings.join(' '));warning.className='note';summary.append(warning)}
if(data.review_evidence?.weak_match_review_suggested){const evidence=data.review_evidence;const warning=node('div',`Review suggested: top match ${evidence.entropy_similarity.toFixed(3)} entropy similarity; ${evidence.matched_peak_count}/${evidence.total_peak_count} experimental peaks matched. The threshold is a heuristic, not identification confidence.`);warning.className='note';summary.append(warning)}
if(!candidates.length){summary.append(node('p','No atlas candidates were ranked. Inspect retrieval.json for the reason.'))}
if(data.denovo_candidates?.length){const section=document.getElementById('denovo');section.append(node('h2','FRIGID proposals'));section.append(node('p','Generated structures are listed separately until scored against the experimental spectrum.'));const list=document.createElement('ol');data.denovo_candidates.forEach(c=>list.append(node('li',`${c.smiles} · formula ${c.formula||'unknown'} · formula match ${c.formula_match}`)));section.append(list)}
candidates.forEach((c,i)=>{const card=node('button','');card.className='card';if(c.structure_image){const pic=document.createElement('img');pic.src=c.structure_image;pic.alt=`Structure ${c.smiles}`;card.append(pic)}card.append(node('h3',`#${i+1} ${c.smiles}`));card.append(node('p',`${c.source} · ${c.formula}`));card.append(node('p',`Entropy similarity: ${c.entropy_similarity.toFixed(3)} · Explained intensity: ${(100*c.explained_intensity).toFixed(1)}%`));card.onclick=()=>{selected=i;render()};cards.append(card)});
function render(){document.querySelectorAll('.card').forEach((e,i)=>e.classList.toggle('active',i===selected));const c=candidates[selected];if(!c)return;
const energies=c.energy_alignment||[];ce.replaceChildren(...energies.map(pair=>{const o=node('option',`${pair.input_value} ${pair.input_unit} → ${pair.experimental_ev.toFixed(2)} eV (atlas ${pair.atlas_ev} eV)`);o.value=pair.experimental_key;return o}));draw()}
function draw(){const svg=document.getElementById('mirror');svg.replaceChildren();const c=candidates[selected];if(!c)return;const pair=(c.energy_alignment||[]).find(p=>p.experimental_key===ce.value);if(!pair)return;const key=pair.experimental_key, ex=data.spectra[key]||[], pred=c.predicted_spectra[pair.atlas_key]||[];
const maxMz=Math.max(data.parentmass,...ex.map(x=>x[0]),...pred.map(x=>x[0]));const maxEx=Math.max(1,...ex.map(x=>x[1])),maxPred=Math.max(1,...pred.map(x=>x[1]));
function line(x1,y1,x2,y2,cls,label){const el=document.createElementNS('http://www.w3.org/2000/svg','line');for(const [k,v] of Object.entries({x1,y1,x2,y2}))el.setAttribute(k,v);el.setAttribute('class',cls);if(label){const t=document.createElementNS('http://www.w3.org/2000/svg','title');t.textContent=label;el.append(t)}svg.append(el)}
line(38,215,980,215,'axis');const matched=new Set(c.matched_peaks.filter(p=>Number(p.ce)===Number(key)).map(p=>p.mz));
ex.forEach(([mz,intensity])=>line(38+940*mz/maxMz,215,38+940*mz/maxMz,215-185*intensity/maxEx,matched.has(mz)?'matched':'exp',`${mz.toFixed(4)}: ${intensity}`));
pred.forEach(([mz,intensity])=>line(38+940*mz/maxMz,215,38+940*mz/maxMz,215+185*intensity/maxPred,'pred',`${mz.toFixed(4)}: ${intensity}`));
document.getElementById('peak-note').textContent=`${matched.size} matched experimental peaks at ${pair.input_value} ${pair.input_unit} (${pair.experimental_ev.toFixed(2)} eV) versus atlas ${pair.atlas_ev} eV (10 ppm or 0.002 Da). Match markers show m/z agreement, not fragment identity.`}
ce.onchange=draw;render();
function saveBlob(blob,name){const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
function svgText(){const copy=document.getElementById('mirror').cloneNode(true);copy.setAttribute('xmlns','http://www.w3.org/2000/svg');const style=document.createElementNS('http://www.w3.org/2000/svg','style');style.textContent='.axis{stroke:#667}.exp{stroke:#175eab}.pred{stroke:#db7036}.matched{stroke:#2b995b}';copy.prepend(style);return new XMLSerializer().serializeToString(copy)}
document.getElementById('save-svg').onclick=()=>saveBlob(new Blob([svgText()],{type:'image/svg+xml'}),`mirror-${ce.value}eV.svg`);
document.getElementById('save-png').onclick=()=>{const img=new Image();const url=URL.createObjectURL(new Blob([svgText()],{type:'image/svg+xml'}));img.onload=()=>{const canvas=document.createElement('canvas');canvas.width=1000;canvas.height=440;const ctx=canvas.getContext('2d');ctx.fillStyle='white';ctx.fillRect(0,0,1000,440);ctx.drawImage(img,0,0);canvas.toBlob(blob=>saveBlob(blob,`mirror-${ce.value}eV.png`));URL.revokeObjectURL(url)};img.src=url};
</script></html>''')
    return target
