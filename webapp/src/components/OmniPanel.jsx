const CONCERN_CLASS = {
  aucune: "concern-none",
  incertain: "concern-unsure",
  chaleur: "concern-heat",
  froid: "concern-cold",
  stress: "concern-stress",
};

function OmniTick({ tick }) {
  const analysis = tick.nano_omni_analysis || {};
  const failed = Boolean(analysis.error);

  return (
    <div className="omni-tick">
      <div className="omni-tick-header">
        <span className="omni-tick-time">t = {tick.timestamp_s.toFixed(1)}s</span>
        <span className="omni-tick-pigs">{tick.n_pigs} pigs detected</span>
      </div>
      {failed ? (
        <p className="omni-error">Vision analysis unavailable: {analysis.error}</p>
      ) : (
        <>
          <p>
            <strong>{analysis.spatial_distribution}</strong>
            {" -- "}
            <span className={CONCERN_CLASS[analysis.possible_concern] || ""}>
              concern: {analysis.possible_concern}
            </span>
          </p>
          <p className="omni-notes">{analysis.clustering_notes}</p>
        </>
      )}
    </div>
  );
}

export default function OmniPanel({ ticks }) {
  return (
    <div className="panel">
      <h2>Vision readings (Nano Omni)</h2>
      <div className="omni-ticks">
        {ticks.map((tick) => (
          <OmniTick key={tick.tick} tick={tick} />
        ))}
      </div>
    </div>
  );
}
