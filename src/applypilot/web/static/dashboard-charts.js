const charts = new Map();
let library;
function chartLibrary() {
  if (!library) library = new Promise((resolve, reject) => {
    if (window.Chart) { resolve(window.Chart); return; }
    const script = document.createElement('script');
    script.src = '/static/chart-4.5.1.umd.min.js';
    script.onload = () => resolve(window.Chart);
    script.onerror = () => { script.remove(); library = null; reject(new Error('Chart library unavailable')); };
    document.head.append(script);
  });
  return library;
}
function colors() {
  const style = getComputedStyle(document.documentElement);
  return { text: style.getPropertyValue('--muted').trim(), line: style.getPropertyValue('--line').trim(), palette: ['#4385d1', '#21a487', '#b481cb'] };
}
export function cleanupCharts(root = document) {
  for (const [canvas, instance] of charts) {
    if (!canvas.isConnected || canvas === root || root.contains(canvas)) { instance.destroy(); charts.delete(canvas); }
  }
}
export async function mountCharts(root = document) {
  const canvases = [...(root.matches?.('[data-chart]') ? [root] : []), ...root.querySelectorAll('[data-chart]')];
  if (!canvases.length) return;
  let Chart;
  try { Chart = await chartLibrary(); } catch { return; } // The HTML table remains usable.
  await document.fonts.ready;
  const fontFamily = getComputedStyle(document.body).fontFamily;
  const theme = colors();
  for (const canvas of canvases) {
    if (!canvas.isConnected || charts.has(canvas)) continue;
    let spec;
    try { spec = JSON.parse(canvas.dataset.chart); } catch { continue; }
    const rows = spec.rows || [];
    const horizontal = spec.type === 'bar';
    const stamps = spec.timeseries ? [...new Set(spec.timeseries.flatMap(series => series.points.map(point => point.at)))].sort((a,b) => a-b) : [];
    const labels = spec.timeseries ? stamps.map(at => new Intl.DateTimeFormat(undefined,{hour:'2-digit',minute:'2-digit'}).format(new Date(at*1000)))
      : rows.map(row => row.date ? new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',timeZone:'UTC'}).format(new Date(row.date + 'T00:00:00Z')) : String(row.label || '').replaceAll('_',' '));
    const datasets = (spec.timeseries || spec.datasets || []).map((dataset,index) => {
      const points = new Map((dataset.points || []).map(point => [point.at,point.value]));
      return {label: dataset.label, data: spec.timeseries ? stamps.map(at => points.get(at) ?? null) : rows.map(row => Number(row[dataset.key]) || 0), borderColor: theme.palette[index % theme.palette.length], backgroundColor: theme.palette[index % theme.palette.length] + (horizontal ? 'cf' : '14'), borderWidth: horizontal ? 0 : 2, borderRadius: horizontal ? 3 : 0, pointRadius: 0, pointHitRadius: 12, tension: 0.25, fill: !horizontal && index === 0, maxBarThickness: 26};
    });
    const instance = new Chart(canvas, {type: spec.type, data: {labels,datasets}, options: {responsive: true, maintainAspectRatio: false, animation: false, indexAxis: horizontal ? 'y' : 'x', interaction: {mode:'index',intersect:false}, plugins: {legend: {display:!horizontal,position:'bottom',align:'start',labels:{usePointStyle:true,boxWidth:7,boxHeight:7,padding:20,color:theme.text,font:{family:fontFamily,size:11}}}, tooltip:{padding:10,bodyFont:{family:fontFamily},titleFont:{family:fontFamily}}}, scales: {x:{beginAtZero:true,grid:{color:horizontal?theme.line:'transparent'},border:{display:false},ticks:{color:theme.text,maxTicksLimit:7,precision:0,font:{family:fontFamily,size:10}}},y:{beginAtZero:true,grid:{color:horizontal?'transparent':theme.line},border:{display:false},ticks:{color:theme.text,precision:0,font:{family:fontFamily,size:10}}}}}});
    charts.set(canvas, instance);
  }
}
document.addEventListener('ap:theme', () => { cleanupCharts(); mountCharts(); });
