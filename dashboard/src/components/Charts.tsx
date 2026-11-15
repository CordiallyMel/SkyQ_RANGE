import type { FuturesRow, MonthlyPoint } from "../types";
import { compactCurrency, currency } from "../lib/format";

export function MonthlyBars({ data }: { data: MonthlyPoint[] }) {
  if (data.length === 0) return <div className="chart-empty">No monthly data.</div>;
  const width = 920;
  const height = 190;
  const pad = { left: 54, right: 12, top: 12, bottom: 30 };
  const values = data.map((point) => point.net);
  const maxAbs = Math.max(1, ...values.map((value) => Math.abs(value)));
  const baseline = pad.top + (height - pad.top - pad.bottom) / 2;
  const barWidth = Math.max(2, (width - pad.left - pad.right) / data.length - 2);
  const step = (width - pad.left - pad.right) / data.length;

  return (
    <div className="monthly-bars">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Monthly net P&L">
        <line
          x1={pad.left}
          x2={width - pad.right}
          y1={baseline}
          y2={baseline}
          stroke="rgba(148, 163, 184, 0.3)"
        />
        {data.map((point, index) => {
          const magnitude = (Math.abs(point.net) / maxAbs) * ((height - pad.top - pad.bottom) / 2);
          const x = pad.left + index * step + (step - barWidth) / 2;
          const y = point.net >= 0 ? baseline - magnitude : baseline;
          return (
            <rect
              key={point.month}
              x={x}
              y={y}
              width={barWidth}
              height={Math.max(1, magnitude)}
              rx="1.5"
              fill={point.net >= 0 ? "#2dd4bf" : "#fb7185"}
              opacity={point.net >= 0 ? 0.76 : 0.82}
            >
              <title>{`${point.month}: ${currency(point.net)}`}</title>
            </rect>
          );
        })}
        {data.map((point, index) => {
          if (index % 36 !== 0 && index !== data.length - 1) return null;
          const x = pad.left + index * step + step / 2;
          return (
            <text key={`label-${point.month}`} x={x} y={height - 7} textAnchor="middle" className="axis-label">
              {point.month.slice(0, 4)}
            </text>
          );
        })}
        <text x={pad.left - 10} y={pad.top + 5} textAnchor="end" className="axis-label">
          {compactCurrency(maxAbs)}
        </text>
        <text x={pad.left - 10} y={baseline + 4} textAnchor="end" className="axis-label">
          $0
        </text>
        <text x={pad.left - 10} y={height - pad.bottom - 2} textAnchor="end" className="axis-label">
          {compactCurrency(-maxAbs)}
        </text>
      </svg>
    </div>
  );
}

export function GateBars({ rows }: { rows: FuturesRow[] }) {
  if (rows.length === 0) return <div className="chart-empty">No futures gate data.</div>;
  const maxAbs = Math.max(1, ...rows.map((row) => Math.abs(row.net_per_month)));
  return (
    <div className="gate-bars">
      {rows.map((row) => {
        const width = (Math.abs(row.net_per_month) / maxAbs) * 100;
        return (
          <div className="gate-row" key={row.symbol}>
            <div className="gate-symbol">
              <strong>{row.symbol}</strong>
              <span>{row.trades.toLocaleString()} trades</span>
            </div>
            <div className="gate-track">
              <div className="gate-negative" style={{ width: `${width}%` }} />
            </div>
            <div className="gate-value">{currency(row.net_per_month)}/mo</div>
          </div>
        );
      })}
    </div>
  );
}
