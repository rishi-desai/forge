// EquityChart.jsx — cumulative equity via TradingView Lightweight Charts (v4),
// baseline series so above-start renders green and below-start renders red.
import React, { useEffect, useRef } from "react";
import { createChart } from "lightweight-charts";

export default function EquityChart({ curve = [], height = 260 }) {
  const ref = useRef(null);

  useEffect(() => {
    if (!ref.current || curve.length === 0) return;
    const chart = createChart(ref.current, {
      height,
      layout: { background: { color: "transparent" }, textColor: "#8A99B5",
                fontFamily: "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif" },
      grid: { vertLines: { color: "#1F2A40" }, horzLines: { color: "#1F2A40" } },
      rightPriceScale: { borderColor: "#1F2A40" },
      timeScale: { borderColor: "#1F2A40" },
      crosshair: { mode: 1 },
      handleScroll: false, handleScale: false,
    });
    const base = curve[0].equity;
    const series = chart.addBaselineSeries({
      baseValue: { type: "price", price: base },
      topFillColor1: "rgba(52,211,153,0.25)", topFillColor2: "rgba(52,211,153,0.02)",
      topLineColor: "#34D399",
      bottomFillColor1: "rgba(248,113,113,0.02)", bottomFillColor2: "rgba(248,113,113,0.25)",
      bottomLineColor: "#F87171",
      lineWidth: 2,
    });
    const seen = new Set();
    const data = curve.flatMap((p) => {
      const time = p.ts.slice(0, 10);
      if (seen.has(time)) return [];
      seen.add(time);
      return [{ time, value: p.equity }];
    });
    series.setData(data);
    chart.timeScale().fitContent();
    const ro = new ResizeObserver(() => chart.applyOptions({ width: ref.current.clientWidth }));
    ro.observe(ref.current);
    return () => { ro.disconnect(); chart.remove(); };
  }, [curve, height]);

  return <div ref={ref} className="w-full" />;
}
