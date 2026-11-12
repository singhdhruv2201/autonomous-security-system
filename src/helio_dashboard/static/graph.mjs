import {escapeHTML as h, graphNodeLabel, initials} from './model.mjs';

function titleLines(title) {
  const words=title.split(/\s+/), lines=[''];
  for(const word of words) {
    const i=lines.length-1;
    if(lines[i] && (lines[i]+' '+word).length>21) lines.push(word);
    else lines[i]+=(lines[i]?' ':'')+word;
  }
  return lines.slice(0,2).map((line,i)=>line.length>21 || (i===1 && lines.length>2) ? line.slice(0,20)+'…' : line);
}
export function graphEdgePath(a,b) {
  if(a.x===b.x) return `M${a.x+98} ${a.y} C${a.x+122} ${a.y},${b.x+122} ${b.y},${b.x+98} ${b.y}`;
  const direction=b.x>a.x?1:-1, start=a.x+98*direction, end=b.x-98*direction, mid=(start+end)/2;
  return `M${start} ${a.y} C${mid} ${a.y},${mid} ${b.y},${end} ${b.y}`;
}
export function renderGraphNode(n,{color,icon,selected=false,dim=false}) {
  const label=graphNodeLabel(n),lines=titleLines(label.title),person=['Employee','AppAccount'].includes(n.kind);
  return `<g class="graph-node ${selected?'selected':''} ${dim?'dim':''}" transform="translate(${n.x} ${n.y})" role="button" tabindex="0" aria-label="${h(label.title)}, ${h(label.subtitle)}" data-action="graph-node" data-id="${h(n.id)}"><title>${h(label.title)} · ${h(label.subtitle)} · ${h(n.source_id)}</title><rect class="node-surface" x="-98" y="-31" width="196" height="62" rx="10" fill="#fff" stroke="${h(color)}"/><g class="node-content">${person?`<circle cx="-76" cy="0" r="14" fill="${h(color)}"/><text class="node-initials" x="-76" y="4">${h(initials(label.title))}</text>`:`<g transform="translate(-87 -11)" style="color:${h(color)}">${icon}</g>`}<text class="node-label" x="-54" y="${lines.length>1?-12:-4}">${lines.map((line,i)=>`<tspan x="-54" dy="${i?16:0}">${h(line)}</tspan>`).join('')}</text><text class="node-subtitle" x="-54" y="22">${h(label.subtitle)}</text>${n.status==='inactive'?'<circle cx="-87" cy="-20" r="5" fill="#b46836" stroke="#fff" stroke-width="2"/>':''}</g></g>`;
}
