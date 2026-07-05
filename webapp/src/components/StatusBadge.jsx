const TIER_STYLE = {
  NOMINAL: { bg: "#1f7a3d", label: "NOMINAL" },
  WATCH: { bg: "#b58900", label: "WATCH" },
  WARNING: { bg: "#c9660a", label: "WARNING" },
  CRITICAL: { bg: "#c0392b", label: "CRITICAL" },
};

export default function StatusBadge({ status }) {
  const style = TIER_STYLE[status] || { bg: "#666", label: status || "UNKNOWN" };
  return (
    <span className="status-badge" style={{ backgroundColor: style.bg }}>
      {style.label}
    </span>
  );
}
