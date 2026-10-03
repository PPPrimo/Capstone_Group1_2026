/**
 * LineChart — a lightweight, self-contained canvas line chart.
 *
 * Usage (drop into any div):
 *
 *   const chart = new LineChart(containerDiv, {
 *     data:   [{x: <number|ms-timestamp>, y: <number|null>}, ...],
 *     xLabel: 'Time',       // optional — label shown below the x axis
 *     yLabel: 'CPU Usage',  // optional — chart title / y axis description
 *     yMax:   100,          // optional — fixed y max; auto-computed if omitted
 *     yUnit:  '%',          // optional — appended to y tick labels (e.g. '%', '°C')
 *     color:  '#007aff',    // optional — line color   (default: '#007aff')
 *     fill:   'rgba(...)',  // optional — fill color   (auto-derived from color)
 *     height: 180,          // optional — canvas height in px (default: 180)
 *   });
 *
 *   chart.update(newDataArray);  // replace data and redraw
 *   chart.resize();              // redraw at current container size (call on layout change)
 */
class LineChart {
  constructor(container, options = {}) {
    this._container = container;
    this._opts = options;
    this._data = options.data || [];

    this._canvas = document.createElement('canvas');
    container.appendChild(this._canvas);

    this._draw();
  }

  /** Replace the dataset and redraw. */
  update(data) {
    this._data = data || [];
    this._draw();
  }

  /** Redraw at the current container size (call after layout changes). */
  resize() {
    this._draw();
  }

  // ── private ──────────────────────────────────────────────────────────────

  _draw() {
    const opts    = this._opts;
    const data    = this._data;
    const canvas  = this._canvas;
    const dpr     = window.devicePixelRatio || 1;
    const W       = this._container.getBoundingClientRect().width || 300;
    const H       = opts.height || 180;

    canvas.width        = W * dpr;
    canvas.height       = H * dpr;
    canvas.style.width  = W + 'px';
    canvas.style.height = H + 'px';

    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);

    const yLabel  = opts.yLabel || '';
    const xLabel  = opts.xLabel || '';
    const PAD_L   = 48, PAD_R = 14, PAD_T = 24, PAD_B = 32;
    const plotW   = W - PAD_L - PAD_R;
    const plotH   = H - PAD_T - PAD_B;

    // Background
    ctx.fillStyle = '#fafafa';
    ctx.fillRect(0, 0, W, H);

    // Chart title
    ctx.fillStyle = '#333';
    ctx.font = 'bold 11px system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(yLabel, PAD_L + 4, 16);

    // Empty / no-data states
    const valid = data.filter(p => p.y != null);
    if (!valid.length) {
      const msg = data.length ? 'No data available' : 'N/A';
      ctx.fillStyle = '#bbb';
      ctx.font = '14px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(msg, W / 2, H / 2 + 5);
      return;
    }

    const yUnit  = opts.yUnit || '';
    const yMax   = opts.yMax != null ? opts.yMax : (Math.max(...valid.map(p => p.y)) * 1.1 || 1);
    const ySteps = yMax <= 110 ? 4 : 5;

    // Y grid + tick labels
    ctx.lineWidth = 1;
    ctx.font = '10px system-ui, sans-serif';
    for (let i = 0; i <= ySteps; i++) {
      const val = (yMax / ySteps) * i;
      const y   = PAD_T + plotH - (val / yMax) * plotH;
      ctx.strokeStyle = '#e8e8e8';
      ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + plotW, y); ctx.stroke();
      ctx.fillStyle = '#999';
      ctx.textAlign = 'right';
      ctx.fillText(Math.round(val) + yUnit, PAD_L - 5, y + 3);
    }

    // X tick labels
    const xMin   = data[0].x;
    const xMax   = data[data.length - 1].x;
    const xSpan  = xMax - xMin || 1;
    const xIsMs  = xMax > 1e12; // treat as ms-timestamp if x looks like epoch ms
    const ticks  = Math.min(5, data.length);
    ctx.textAlign = 'center';
    ctx.fillStyle = '#999';
    ctx.font = '10px system-ui, sans-serif';
    for (let i = 0; i <= ticks; i++) {
      const ratio = i / ticks;
      const x     = PAD_L + ratio * plotW;
      let label;
      if (xIsMs) {
        const d = new Date(xMin + ratio * xSpan);
        label = d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
      } else {
        label = (xMin + ratio * xSpan).toFixed(1);
      }
      ctx.fillText(label, x, PAD_T + plotH + 16);
    }

    // Optional x-axis label
    if (xLabel) {
      ctx.fillStyle = '#555';
      ctx.font = '10px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(xLabel, PAD_L + plotW / 2, H - 3);
    }

    // Auto-detect gap threshold: 3× median inter-sample gap
    let gapThresh = Infinity;
    if (data.length > 1) {
      const gaps = [];
      for (let i = 1; i < Math.min(data.length, 201); i++) gaps.push(data[i].x - data[i - 1].x);
      gaps.sort((a, b) => a - b);
      gapThresh = gaps[Math.floor(gaps.length / 2)] * 3;
    }

    // Downsample for performance (max ~1500 rendered points)
    const step = data.length > 1500 ? Math.floor(data.length / 1500) : 1;

    // Build segments split by null values or gaps
    const segments = [];
    let seg = [], prevX = null;
    for (let i = 0; i < data.length; i += step) {
      const { x, y } = data[i];
      if (y == null) {
        if (seg.length) { segments.push(seg); seg = []; }
        prevX = null;
        continue;
      }
      if (prevX != null && (x - prevX) > gapThresh) {
        if (seg.length) { segments.push(seg); seg = []; }
      }
      seg.push({
        x: PAD_L + ((x - xMin) / xSpan) * plotW,
        y: PAD_T + plotH - (Math.min(y, yMax) / yMax) * plotH,
      });
      prevX = x;
    }
    if (seg.length) segments.push(seg);

    // Derive fill color from line color if not provided
    const lineColor  = opts.color || '#007aff';
    let   fillColor  = opts.fill;
    if (!fillColor && /^#[0-9a-f]{6}$/i.test(lineColor)) {
      const r = parseInt(lineColor.slice(1, 3), 16);
      const g = parseInt(lineColor.slice(3, 5), 16);
      const b = parseInt(lineColor.slice(5, 7), 16);
      fillColor = `rgba(${r},${g},${b},0.10)`;
    }
    fillColor = fillColor || 'rgba(0,122,255,0.10)';

    // Draw each segment
    const baseY = PAD_T + plotH;
    ctx.strokeStyle = lineColor;
    ctx.lineWidth   = 1.5;
    ctx.lineJoin    = 'round';
    for (const s of segments) {
      ctx.beginPath();
      ctx.moveTo(s[0].x, s[0].y);
      for (let j = 1; j < s.length; j++) ctx.lineTo(s[j].x, s[j].y);
      ctx.stroke();
      ctx.lineTo(s[s.length - 1].x, baseY);
      ctx.lineTo(s[0].x, baseY);
      ctx.closePath();
      ctx.fillStyle = fillColor;
      ctx.fill();
    }
  }
}
