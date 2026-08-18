/** @type {import('tailwindcss').Config} */

// Tailwind v3 on purpose, not v4: v4's CSS-first config would have meant
// rewriting the palette at the same time as removing the CDN, and those are
// two different changes.
//
// --- The overhaul palette ---------------------------------------------------
// The app icon leads: deep navy court, red paddle, amber ball. Three colours
// carry meaning and never drift -- red is the action you can take, amber means
// live right now and appears nowhere else, green means confirmed.
//
// Every colour resolves through a CSS custom property holding a bare `R G B`
// triple, declared in app.css. Two reasons:
//
//   1. Dark is the base palette and light is the override, driven by the
//      viewer's system setting. Only the *surfaces* flip; paddle, ball,
//      confirmed and the player hues are identities, not surfaces, and are
//      identical in both themes.
//   2. The triples keep Tailwind's slash-opacity syntax working, and the app
//      uses it heavily (bg-primary/10, bg-success/10, bg-muted/30).
//
// The semantic names (background, card, muted, primary, ...) are kept and
// remapped rather than replaced, so the screens this round does not touch
// re-skin correctly instead of rendering light-on-light.
const withOpacity = (variable) => `rgb(var(${variable}) / <alpha-value>)`;

module.exports = {
  content: [
    // Every template, including registration/ which used to load its own CDN
    // copy of Tailwind with a different palette.
    './ttstats/pingpong/templates/**/*.html',
    // Class strings still built in Python (forms.py widget attrs). Tailwind
    // cannot see these unless the file is scanned, and a purged class fails
    // silently -- no error, just an unstyled input.
    './ttstats/pingpong/*.py',
  ],
  theme: {
    extend: {
      colors: {
        // --- Raw design tokens, named as the design names them -------------
        court: {
          DEFAULT: withOpacity('--court'),
          raised: withOpacity('--court-raised'),
          line: withOpacity('--court-line'),
          overlay: withOpacity('--court-overlay'),
          edge: withOpacity('--court-edge'),
          foreground: withOpacity('--bone'),
        },
        bone: {
          DEFAULT: withOpacity('--bone'),
          dim: withOpacity('--bone-dim'),
          muted: withOpacity('--bone-muted'),
        },
        // The action you can take. Nothing else is this red.
        paddle: {
          DEFAULT: withOpacity('--paddle'),
          600: withOpacity('--paddle-600'),
          700: withOpacity('--paddle-700'),
          foreground: withOpacity('--paddle-foreground'),
        },
        // Live, right now. Appears nowhere else -- not on warnings, not on
        // "pending", not as a highlight.
        ball: {
          DEFAULT: withOpacity('--ball'),
          foreground: withOpacity('--ball-foreground'),
        },
        confirmed: {
          DEFAULT: withOpacity('--confirmed'),
          foreground: withOpacity('--confirmed-foreground'),
        },

        // --- Semantic aliases, so untouched screens re-skin -----------------
        border: withOpacity('--court-line'),
        input: withOpacity('--court-line'),
        ring: withOpacity('--paddle'),
        background: withOpacity('--court'),
        foreground: withOpacity('--bone'),
        card: {
          DEFAULT: withOpacity('--court-raised'),
          foreground: withOpacity('--bone'),
        },
        popover: {
          DEFAULT: withOpacity('--court-overlay'),
          foreground: withOpacity('--bone'),
        },
        primary: {
          DEFAULT: withOpacity('--paddle'),
          foreground: withOpacity('--paddle-foreground'),
        },
        secondary: {
          DEFAULT: withOpacity('--court-line'),
          foreground: withOpacity('--bone'),
        },
        destructive: {
          DEFAULT: withOpacity('--paddle'),
          foreground: withOpacity('--paddle-foreground'),
        },
        muted: {
          DEFAULT: withOpacity('--court-raised'),
          foreground: withOpacity('--bone-muted'),
        },
        accent: {
          DEFAULT: withOpacity('--court-line'),
          foreground: withOpacity('--bone'),
        },
        success: {
          DEFAULT: withOpacity('--confirmed'),
          foreground: withOpacity('--confirmed-foreground'),
        },
        warning: {
          DEFAULT: withOpacity('--ball'),
          foreground: withOpacity('--ball-foreground'),
        },
      },

      // Radius 0 everywhere, `full` included -- an app of hard 2px rules with
      // pill-shaped chips and circular avatars reads as two designs sharing a
      // page. Keeping the keys defined-but-square means the ~200 existing
      // `rounded-lg` classes go flat instead of erroring, and no template has
      // to be edited to remove one.
      //
      // The single exception is the live ball, which is a circle because it is
      // a ball. It gets its radius from `.dot` in app.css rather than from a
      // utility, precisely so that this table can stay absolute.
      borderRadius: {
        DEFAULT: '0px',
        none: '0px',
        sm: '0px',
        md: '0px',
        lg: '0px',
        xl: '0px',
        '2xl': '0px',
        '3xl': '0px',
        full: '0px',
      },

      fontFamily: {
        // Archivo, self-hosted from /static/pingpong/fonts (OFL). Declared as
        // the default sans so every existing screen picks it up without being
        // touched.
        sans: ['Archivo', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },

      fontSize: {
        // The design's type scale, named as it names it. Each is [size,
        // {lineHeight, letterSpacing, fontWeight}] so a single class carries
        // the whole specimen -- there is no such thing as a 34px display in a
        // different weight.
        'score-hero': ['150px', { lineHeight: '0.8', letterSpacing: '-0.045em', fontWeight: '800' }],
        score: ['44px', { lineHeight: '1', letterSpacing: '-0.045em', fontWeight: '800' }],
        display: ['34px', { lineHeight: '1.05', letterSpacing: '-0.03em', fontWeight: '800' }],
        title: ['22px', { lineHeight: '1.15', letterSpacing: '-0.02em', fontWeight: '800' }],
        heading: ['17px', { lineHeight: '1.2', letterSpacing: '-0.01em', fontWeight: '800' }],
        body: ['15px', { lineHeight: '1.5', fontWeight: '400' }],
        small: ['13px', { lineHeight: '1.45', fontWeight: '600' }],
        label: ['11px', { lineHeight: '1', letterSpacing: '0.14em', fontWeight: '800' }],
      },

      spacing: {
        // Safe areas, as the design specifies them, with the env() actual
        // value winning where the device reports one.
        'safe-top': 'env(safe-area-inset-top, 0px)',
        'safe-bottom': 'env(safe-area-inset-bottom, 0px)',
        // Height of the bottom tab bar + its safe area. Content padding and
        // the bar's own height must not be able to disagree.
        tabbar: 'calc(56px + env(safe-area-inset-bottom, 0px))',
      },

      boxShadow: {
        // Dark themes cannot use shadow for elevation -- there is nothing for
        // a shadow to fall on. A raised surface is a hard bottom rule instead.
        raised: '0 2px 0 rgb(var(--court-line))',
        overlay: '0 -2px 0 rgb(var(--paddle)), var(--overlay-shadow)',
      },
    },
  },
  plugins: [],
};
