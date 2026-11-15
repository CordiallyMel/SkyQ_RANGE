import { useMemo, useState, type MouseEvent } from "react";
import type { EquityPoint } from "../types";
import { compactCurrency, compactDate, currency } from "../lib/format";

const W = 920;
const H = 310;
const PAD = { left: 68, right: 18, top: 18, bottom: 34 };

export function EquityChart({
  points,
  startingEquity,
}: {
  points: EquityPoint[];
  startingEquity: number;
}) {
  const [active, setActive] = useState<number | null>(null);

  const chart = useMemo(() => {
    if (points.length === 0) return null;
    const values = points.map((point) => point.equity);
    const rawMin = Math.min(...values, startingEquity);
    const rawMax = Math.max(...values, startingEquity);
    const spread = Math.max(1, rawMax - rawMin);
    const min = rawMin - spread * 0.08;
    const max = rawMax + spread * 0.08;
    const x = (index: number) =>
      PAD.left +
      (points.length === 1
        ? 0
        : (index / (points.length - 1)) * (W - PAD.left - PAD.right));
    const y = (value: number) =>
      PAD.top + ((max - value) / (max - min)) * (H - PAD.top - PAD.bottom);
    const line = points
      .map((point, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(point.equity).toFixed(1)}`)
      .join(" ");
    const area = `${line} L${x(points.length - 1).toFixed(1)},${H - PAD.bottom} L${x(0).toFixed(1)},${H - PAD.bottom} Z`;
    const ticks = Array.from({ length: 5 }, (_, index) => {
      const value = max - ((max - min) / 4) * index;
      return { value, y: y(value) };
    });
    return { x, y, line, area, ticks, min, max };
  }, [points, startingEquity]);

  if (!chart) {
    return <div className="chart-empty">No equity points available.</div>;
  }

  function handleMove(event: MouseEvent<SVGRectElement>) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const relative = ((event.clientX - bounds.left) / bounds.width) * W;
    const usable = W - PAD.left - PAD.right;
    const index = Math.round(((relative - PAD.left) / usable) * (points.length - 1));
    setActive(Math.max(0, Math.min(points.length - 1, index)));
  }

  const selected = active === null ? null : points[active];
  const tooltipWidth = 178;
  const tooltipX =
    active === null
      ? 0
      : Math.min(
          W - PAD.right - tooltipWidth,
          Math.max(PAD.left, chart.x(active) - tooltipWidth / 2),
        );

  return (
    <div className="equity-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Closed-trade equity curve">
        <defs>
          <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#5eead4" stopOpacity="0.26" />
            <stop offset="100%" stopColor="#5eead4" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="equity-stroke" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#2dd4bf" />
            <stop offset="100%" stopColor="#a7f3d0" />
          </linearGradient>
        </defs>
        {chart.ticks.map((tick) => (
          <g key={tick.value}>
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={tick.y}
              y2={tick.y}
              stroke="rgba(148, 163, 184, 0.12)"
              strokeDasharray="3 6"
            />
            <text x={PAD.left - 12} y={tick.y + 4} textAnchor="end" className="axis-label">
              {compactCurrency(tick.value)}
            </text>
          </g>
        ))}
        <line
          x1={PAD.left}
          x2={W - PAD.right}
          y1={chart.y(startingEquity)}
          y2={chart.y(startingEquity)}
          stroke="rgba(251, 191, 36, 0.45)"
          strokeDasharray="5 5"
        />
        <text
          x={W - PAD.right}
          y={chart.y(startingEquity) - 7}
          textAnchor="end"
          className="axis-label axis-label-amber"
        >
          $1M bankroll
        </text>
        <path d={chart.area} fill="url(#equity-fill)" />
        <path d={chart.line} fill="none" stroke="url(#equity-stroke)" strokeWidth="2.2" />
        <text x={PAD.left} y={H - 8} className="axis-label">
          {compactDate(points[0].date)}
        </text>
        <text x={W - PAD.right} y={H - 8} textAnchor="end" className="axis-label">
          {compactDate(points[points.length - 1].date)}
        </text>
        {selected && active !== null && (
          <g>
            <line
              x1={chart.x(active)}
              x2={chart.x(active)}
              y1={PAD.top}
              y2={H - PAD.bottom}
              stroke="rgba(226, 232, 240, 0.32)"
              strokeDasharray="3 4"
            />
            <circle
              cx={chart.x(active)}
              cy={chart.y(selected.equity)}
              r="4.5"
              fill="#07120f"
              stroke="#5eead4"
              strokeWidth="2"
            />
            <g transform={`translate(${tooltipX}, ${PAD.top + 8})`}>
              <rect width={tooltipWidth} height="58" rx="7" fill="#0b1521" stroke="rgba(148,163,184,.24)" />
              <text x="12" y="20" className="tooltip-label">
                {compactDate(selected.date)}
              </text>
              <text x="12" y="40" className="tooltip-value">
                {currency(selected.equity)}
              </text>
              <text x={tooltipWidth - 12} y="40" textAnchor="end" className="tooltip-sub">
                {selected.drawdown === 0 ? "at high" : `${currency(selected.drawdown)} DD`}
              </text>
            </g>
          </g>
        )}
        <rect
          x={PAD.left}
          y={PAD.top}
          width={W - PAD.left - PAD.right}
          height={H - PAD.top - PAD.bottom}
          fill="transparent"
          onMouseMove={handleMove}
          onMouseLeave={() => setActive(null)}
        />
      </svg>
    </div>
  );
}
