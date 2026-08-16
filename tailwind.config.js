/** @type {import('tailwindcss').Config} */

// Tailwind v3 on purpose, not v4: the theme below is a straight lift of the
// `tailwind.config` object that used to live inline in base.html, which is v3
// shaped. v4's CSS-first config would have meant rewriting the palette at the
// same time as removing the CDN, and those are two different changes.
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
        border: 'hsl(214.3 31.8% 91.4%)',
        input: 'hsl(214.3 31.8% 91.4%)',
        ring: 'hsl(222.2 84% 4.9%)',
        background: 'hsl(0 0% 100%)',
        foreground: 'hsl(222.2 84% 4.9%)',
        primary: {
          DEFAULT: 'hsl(222.2 47.4% 11.2%)',
          foreground: 'hsl(210 40% 98%)',
        },
        secondary: {
          DEFAULT: 'hsl(210 40% 96.1%)',
          foreground: 'hsl(222.2 47.4% 11.2%)',
        },
        destructive: {
          DEFAULT: 'hsl(0 84.2% 60.2%)',
          foreground: 'hsl(210 40% 98%)',
        },
        muted: {
          DEFAULT: 'hsl(210 40% 96.1%)',
          foreground: 'hsl(215.4 16.3% 46.9%)',
        },
        accent: {
          DEFAULT: 'hsl(210 40% 96.1%)',
          foreground: 'hsl(222.2 47.4% 11.2%)',
        },
        success: {
          DEFAULT: 'hsl(142.1 76.2% 36.3%)',
          foreground: 'hsl(0 0% 100%)',
        },
        warning: {
          DEFAULT: 'hsl(38 92% 50%)',
          foreground: 'hsl(0 0% 100%)',
        },
      },
      borderRadius: {
        lg: '0.5rem',
        md: 'calc(0.5rem - 2px)',
        sm: 'calc(0.5rem - 4px)',
      },
    },
  },
  plugins: [],
};
