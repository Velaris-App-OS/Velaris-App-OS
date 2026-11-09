/* HxGraph visualizer — the script of the page HxGraph.tsx builds (srcdoc iframe).
   A static file Studio serves, not an inline script, so the page works under
   Studio's Content-Security-Policy (script-src 'self', 2026-10-29). The graph
   and the colours arrive as JSON data blocks, which are never executed.
   Copyright (c) 2024-2026 Velaris Contributors · SPDX-License-Identifier: BSL-1.1 */
if (!window.d3) { const m = document.getElementById("d3-missing"); if (m) m.style.display = "block"; }
else {
const RAW=JSON.parse(document.getElementById("viz-data").textContent);
const C=JSON.parse(document.getElementById("viz-theme").textContent);
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const NC={
  case_type:"#818cf8",stage:"#34d399",step:"#60a5fa",form:"#f59e0b",
  field:"#fb923c",module:"#a78bfa",endpoint:"#94a3b8",access_group:"#f472b6",
  access_role:"#e11d48",connector:"#2dd4bf",concept:"#e879f9",pattern:"#fbbf24"
};
let nodes=RAW.nodes.map(n=>({...n}));
let links=RAW.edges.map(e=>({...e,source:e.from,target:e.to}));
const types=[...new Set(nodes.map(n=>n.node_type))].sort();
const sel=document.getElementById("typeFilter");
types.forEach(t=>{const o=document.createElement("option");o.value=t;o.textContent=t;sel.appendChild(o);});
document.getElementById("stats").textContent=nodes.length+" nodes · "+links.length+" edges";

const svg=d3.select("#canvas");
const w=window.innerWidth,h=window.innerHeight-48;
svg.attr("width",w).attr("height",h);
const g=svg.append("g");
const zoom=d3.zoom().scaleExtent([0.05,4]).on("zoom",e=>g.attr("transform",e.transform));
svg.call(zoom);

const sim=d3.forceSimulation(nodes)
  .force("link",d3.forceLink(links).id(d=>d.id).distance(d=>d.edge_type==="similar_to"?120:55).strength(0.4))
  .force("charge",d3.forceManyBody().strength(-160))
  .force("center",d3.forceCenter(w/2,h/2))
  .force("collision",d3.forceCollide(16));

const link=g.append("g").selectAll("line").data(links).join("line")
  .attr("stroke",d=>d.edge_type==="similar_to"?C.linkSimilar:C.linkColor)
  .attr("stroke-width",d=>d.edge_type==="similar_to"?0.5:1.2)
  .attr("stroke-dasharray",d=>d.edge_type==="similar_to"?"3,3":null)
  .attr("opacity",0.7);

const node=g.append("g").selectAll("circle").data(nodes).join("circle")
  .attr("r",d=>d.node_type==="case_type"?10:d.node_type==="module"?7:5)
  .attr("fill",d=>NC[d.node_type]||"#718096")
  .attr("stroke",C.nodeBorder).attr("stroke-width",1.5)
  .call(d3.drag()
    .on("start",(e,d)=>{if(!e.active)sim.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y;})
    .on("drag",(e,d)=>{d.fx=e.x;d.fy=e.y;})
    .on("end",(e,d)=>{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}));

const label=g.append("g").selectAll("text").data(nodes).join("text")
  .text(d=>d.label.length>22?d.label.slice(0,22)+"…":d.label)
  .attr("font-size",9).attr("fill",C.labelColor).attr("dx",8).attr("dy",4);

const tip=document.getElementById("tip");
node.on("mouseover",(e,d)=>{
  tip.style.display="block";tip.style.left=(e.clientX+14)+"px";tip.style.top=(e.clientY-12)+"px";
  const c=NC[d.node_type]||"#718096";
  tip.innerHTML='<span style="background:'+c+'22;color:'+c+';padding:1px 6px;border-radius:4px;font-size:9px;font-weight:700;text-transform:uppercase">'+esc(d.node_type)+'</span><br><strong>'+esc(d.label)+'</strong>'+(d.summary?'<br><span style="color:'+C.mutedColor+'">'+esc(d.summary)+'</span>':'');
}).on("mousemove",e=>{tip.style.left=(e.clientX+14)+"px";tip.style.top=(e.clientY-12)+"px";})
  .on("mouseout",()=>{tip.style.display="none";});

sim.on("tick",()=>{
  link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y).attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
  node.attr("cx",d=>d.x).attr("cy",d=>d.y);
  label.attr("x",d=>d.x).attr("y",d=>d.y);
});

function applyFilters(){
  const q=(document.getElementById("search").value||"").toLowerCase().trim();
  const t=document.getElementById("typeFilter").value;
  let matchCount=0;
  node.attr("opacity",d=>{
    const qMatch=!q||d.label.toLowerCase().includes(q)||(d.node_type||"").toLowerCase().includes(q);
    const tMatch=!t||d.node_type===t;
    if(qMatch&&tMatch){matchCount++;return 1;}
    return 0.06;
  });
  label.attr("opacity",d=>{
    const qMatch=!q||d.label.toLowerCase().includes(q)||(d.node_type||"").toLowerCase().includes(q);
    const tMatch=!t||d.node_type===t;
    return qMatch&&tMatch?1:0.02;
  });
  const cnt=document.getElementById("search-count");
  const nr=document.getElementById("no-results");
  if(q||t){
    cnt.textContent=matchCount+" match"+(matchCount===1?"":"es");
    nr.style.display=matchCount===0?"block":"none";
  }else{
    cnt.textContent="";
    nr.style.display="none";
  }
}

document.getElementById("search").addEventListener("input",applyFilters);
document.getElementById("typeFilter").addEventListener("change",applyFilters);

window.addEventListener("resize",()=>{
  const nw=window.innerWidth,nh=window.innerHeight-48;
  svg.attr("width",nw).attr("height",nh);
  sim.force("center",d3.forceCenter(nw/2,nh/2)).alpha(0.1).restart();
});
}
