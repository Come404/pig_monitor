function Trend({ value, invert = false }) {
  if (Math.abs(value) < 0.02) {
    return <span className="trend trend-flat">→ stable</span>;
  }
  const up = value > 0;
  const bad = invert ? !up : up;
  const arrow = up ? "↑" : "↓";
  return (
    <span className={`trend ${bad ? "trend-bad" : "trend-good"}`}>
      {arrow} {Math.abs(value).toFixed(2)}
    </span>
  );
}

export default function SensorPanel({ sensors }) {
  return (
    <div className="panel">
      <h2>Sensor readings</h2>
      <table className="sensor-table">
        <thead>
          <tr>
            <th>Parameter</th>
            <th>Start</th>
            <th>End</th>
            <th>Trend</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Temperature (°C)</td>
            <td>{sensors.temp_first.toFixed(2)}</td>
            <td>{sensors.temp_last.toFixed(2)}</td>
            <td><Trend value={sensors.temp_trend} /></td>
          </tr>
          <tr>
            <td>Humidity (%)</td>
            <td>{sensors.hum_first.toFixed(1)}</td>
            <td>{sensors.hum_last.toFixed(1)}</td>
            <td><Trend value={sensors.hum_trend} /></td>
          </tr>
          <tr>
            <td>THI (Xin &amp; Harmon)</td>
            <td>{sensors.thi_mean.toFixed(2)}</td>
            <td>{sensors.thi_last.toFixed(2)}</td>
            <td><Trend value={sensors.thi_trend} /></td>
          </tr>
        </tbody>
      </table>
      <p className="panel-footnote">
        Window: {sensors.t_start} → {sensors.t_end} ({sensors.n} measurements)
      </p>
    </div>
  );
}
