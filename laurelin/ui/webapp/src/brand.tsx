// The Laurelin mark: a small golden two-leaf glyph (Laurelin, the golden tree).

export function TreeGlyph({ size = 26 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <path d="M12 22V11" stroke="var(--gold-dim)" strokeWidth="1.6" strokeLinecap="round" />
      <path
        d="M12 13C12 9 9 7.5 6.5 7.5C6.5 11 9 13 12 13Z"
        fill="var(--gold)"
        opacity="0.85"
      />
      <path
        d="M12 13C12 9 15 7.5 17.5 7.5C17.5 11 15 13 12 13Z"
        fill="var(--gold)"
        opacity="0.85"
      />
      <path
        d="M12 10C12 5.5 12 3 12 3C15 5 15 8 12 10Z"
        fill="var(--gold)"
      />
      <circle cx="12" cy="11" r="1.4" fill="var(--gold)" />
    </svg>
  );
}
