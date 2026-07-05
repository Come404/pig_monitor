import { useEffect, useState } from "react";
import { getReport, runPipeline } from "./api";
import SensorPanel from "./components/SensorPanel";
import OmniPanel from "./components/OmniPanel";
import UltraReportPanel from "./components/UltraReportPanel";
import "./App.css";

function App() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    getReport()
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  async function handleRun() {
    setRunning(true);
    setError(null);
    try {
      const result = await runPipeline();
      setData(result);
    } catch (e) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>PigWatch</h1>
        <button onClick={handleRun} disabled={running}>
          {running ? "Running pipeline..." : "Run pipeline"}
        </button>
      </header>

      {error && (
        <div className="banner banner-error">
          {error.startsWith("404") ? 'No run yet -- click "Run pipeline" to start.' : error}
        </div>
      )}

      {loading && <p>Loading...</p>}

      {data && (
        <main className="app-grid">
          <UltraReportPanel report={data.ultra_report} error={data.ultra_error} />
          <SensorPanel sensors={data.sensors} />
          <OmniPanel ticks={data.omni_ticks} />
        </main>
      )}

      {data && (
        <p className="app-footnote">
          Enclosure {data.enclosure_id} · last run{" "}
          {new Date(data.generated_at * 1000).toLocaleTimeString()}
        </p>
      )}
    </div>
  );
}

export default App;
