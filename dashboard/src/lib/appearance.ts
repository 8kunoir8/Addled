/**
 * Theme and accent, applied to the document.
 *
 * Settings -> Appearance used to be two uncontrolled inputs with no handler:
 * picking "Light" changed nothing and was not even remembered. This is the
 * other half - the part that turns a choice into pixels.
 *
 * Everything is expressed as CSS custom properties on <html>, which is what
 * `globals.css` reads. Nothing here sets a Tailwind class on a component:
 * a class is fixed at build time, so it cannot be themed, and doing it by
 * class would mean every component learning about appearance.
 *
 * Kept out of the settings page so the values can be applied before the
 * dashboard paints - see `applyStoredAppearance`, called from the root layout.
 * Applying on the settings page only would mean a themed dashboard flickers
 * back to the dark default on every reload until the user opens Settings.
 */

export type ThemeName = 'dark' | 'light' | 'system';

export type Appearance = {
  theme: ThemeName;
  accent: string;
};

export const DEFAULT_APPEARANCE: Appearance = {
  theme: 'dark',
  accent: '#3380FF',
};

/** `#3380FF` or `3380ff` -> `51, 128, 255`. Returns null for anything else. */
export function hexToRgb(hex: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** Lighten a colour toward white, for the hover shade. */
export function lighten(hex: string, amount = 0.18): string {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  const [r, g, b] = rgb.map((c) => Math.round(c + (255 - c) * amount));
  return `#${[r, g, b].map((c) => c.toString(16).padStart(2, '0')).join('')}`;
}

/** Darken a colour toward black, for the hover shade on a light theme. */
export function darken(hex: string, amount = 0.18): string {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  const [r, g, b] = rgb.map((c) => Math.round(c * (1 - amount)));
  return `#${[r, g, b].map((c) => c.toString(16).padStart(2, '0')).join('')}`;
}

function systemPrefersLight(): boolean {
  try {
    return window.matchMedia('(prefers-color-scheme: light)').matches;
  } catch {
    return false;
  }
}

/** What "system" means right now. */
export function resolveTheme(theme: ThemeName): 'dark' | 'light' {
  if (theme === 'system') return systemPrefersLight() ? 'light' : 'dark';
  return theme === 'light' ? 'light' : 'dark';
}

/**
 * Apply an appearance to the live document.
 *
 * `--accent` drives buttons, links, highlights and the window glow, so one
 * value covers both of the things the user asked for. The derived shades are
 * computed here rather than being three separate settings, because asking a
 * user to pick a hover colour is asking them to do arithmetic.
 */
export function applyAppearance(appearance: Partial<Appearance>): void {
  if (typeof document === 'undefined') return;
  const theme = (appearance.theme ?? DEFAULT_APPEARANCE.theme) as ThemeName;
  const accent = appearance.accent || DEFAULT_APPEARANCE.accent;
  const root = document.documentElement;
  const effective = resolveTheme(theme);

  // The theme switch is a class, the palette is CSS. `theme-light` selects the
  // block in globals.css that redefines every surface token.
  root.classList.toggle('theme-light', effective === 'light');
  root.dataset.theme = effective;

  const rgb = hexToRgb(accent) || hexToRgb(DEFAULT_APPEARANCE.accent)!;
  root.style.setProperty('--accent', accent);
  root.style.setProperty('--accent-rgb', rgb.join(', '));
  root.style.setProperty(
    '--accent-hover',
    effective === 'light' ? darken(accent) : lighten(accent),
  );
  // A translucent wash used behind selected rows and the editor's selection.
  root.style.setProperty('--accent-soft', `rgba(${rgb.join(', ')}, 0.15)`);

  // The frameless window's own chrome (electron/titlebar area) follows too, so
  // the OS-drawn edge does not stay blue while the app turns orange.
  try {
    window.localStorage.setItem('addled.appearance', JSON.stringify({ theme, accent }));
  } catch {
    /* private mode, or storage full - the choice still applies to this view */
  }
}

/**
 * Apply what was chosen last time, before the first paint.
 *
 * Returns the appearance it used so the settings page can show the real
 * current values instead of the defaults.
 */
export function applyStoredAppearance(): Appearance {
  let stored: Partial<Appearance> = {};
  try {
    const raw = window.localStorage.getItem('addled.appearance');
    if (raw) stored = JSON.parse(raw) || {};
  } catch {
    /* unreadable: fall back to the default */
  }
  const appearance: Appearance = {
    theme: (stored.theme as ThemeName) || DEFAULT_APPEARANCE.theme,
    accent: stored.accent || DEFAULT_APPEARANCE.accent,
  };
  applyAppearance(appearance);
  return appearance;
}

/** Follow the OS setting while the user has chosen "System". */
export function watchSystemTheme(getTheme: () => ThemeName, onChange: () => void): () => void {
  let mq: MediaQueryList | null = null;
  try {
    mq = window.matchMedia('(prefers-color-scheme: light)');
  } catch {
    return () => {};
  }
  const handler = () => {
    if (getTheme() === 'system') onChange();
  };
  mq.addEventListener('change', handler);
  return () => mq?.removeEventListener('change', handler);
}
