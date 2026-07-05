import StatusBadge from "./StatusBadge";

export default function UltraReportPanel({ report, error }) {
  if (error && !report) {
    return (
      <div className="panel panel-report panel-report-error">
        <h2>Ultra welfare report</h2>
        <p className="omni-error">Report unavailable: {error}</p>
      </div>
    );
  }

  if (!report) {
    return (
      <div className="panel panel-report">
        <h2>Ultra welfare report</h2>
        <p>No report yet.</p>
      </div>
    );
  }

  return (
    <div className="panel panel-report">
      <div className="panel-report-header">
        <h2>Ultra welfare report</h2>
        <StatusBadge status={report.status} />
      </div>
      <dl className="report-fields">
        <dt>What's happening</dt>
        <dd>{report.whats_happening}</dd>
        <dt>Likely cause</dt>
        <dd>{report.likely_cause}</dd>
        <dt>Recommended action</dt>
        <dd className="report-action">{report.recommended_action}</dd>
      </dl>
    </div>
  );
}
