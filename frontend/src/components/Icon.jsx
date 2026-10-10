const PATHS = {
  book: "M4 5a2 2 0 0 1 2-2h12v16H6a2 2 0 0 0-2 2V5zm2 14h12",
  spark: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3zM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z",
  ticket: "M4 7a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2a2 2 0 0 0 0 4v2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-2a2 2 0 0 0 0-4V7zm10-2v12",
  file: "M7 3h7l5 5v13H7V3zm7 0v5h5M10 13h6M10 17h6",
  chart: "M4 20V10m6 10V4m6 16v-7m4 7H3",
  logout: "M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H4",
  send: "M4 12l16-8-6 16-2-7-8-1z",
  plus: "M12 5v14M5 12h14",
  trash: "M5 7h14M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  retry: "M4 12a8 8 0 0 1 14-5.3L20 9M20 4v5h-5M20 12a8 8 0 0 1-14 5.3L4 15M4 20v-5h5",
  upload: "M12 16V4M7 9l5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3",
  check: "M5 12l5 5 9-10",
  x: "M6 6l12 12M18 6L6 18",
  bolt: "M13 3L5 13h6l-1 8 8-10h-6l1-8z",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z",
  leaf: "M5 19c0-8 6-14 15-14 0 9-6 15-14 15M5 19l7-7",
};

export function Icon({ name, size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name] || ""} />
    </svg>
  );
}
