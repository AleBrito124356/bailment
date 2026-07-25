import type { Config } from "tailwindcss";

/**
 * Every colour is an HSL triple behind a CSS variable, defined twice in globals.css --
 * once for light and once for dark. Nothing in this file hard-codes a hex value.
 *
 * That is not tidiness. A dashboard whose dark theme is a set of `dark:` overrides
 * sprinkled through two hundred components drifts within a week: somebody adds a border
 * and forgets the override, and the dark theme grows a light-grey line. With variables
 * there is one definition of "border" and it is correct in both themes by construction.
 *
 * The palette is zinc plus exactly one accent (#2563EB). The state colours are muted on
 * purpose -- see `stateTone` in src/lib/states.ts for why an orphaned lease is amber and
 * never red.
 */
const config: Config = {
  darkMode: ["class"],
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    container: {
      center: true,
      padding: "2rem",
    },
    extend: {
      colors: {
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        surface: "hsl(var(--surface))",
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
          soft: "hsl(var(--accent-soft))",
        },
        ok: {
          DEFAULT: "hsl(var(--ok))",
          soft: "hsl(var(--ok-soft))",
        },
        warn: {
          DEFAULT: "hsl(var(--warn))",
          soft: "hsl(var(--warn-soft))",
        },
        danger: {
          DEFAULT: "hsl(var(--danger))",
          soft: "hsl(var(--danger-soft))",
        },
        info: {
          DEFAULT: "hsl(var(--info))",
          soft: "hsl(var(--info-soft))",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "Liberation Mono",
          "monospace",
        ],
      },
      fontSize: {
        // One step below Tailwind's xs, for table meta and label eyebrows. Used sparingly.
        "2xs": ["0.6875rem", { lineHeight: "1rem", letterSpacing: "0.02em" }],
      },
      boxShadow: {
        // Shadows are almost never used. These two exist for things that genuinely float:
        // the request dialog, and the token popover.
        overlay: "0 16px 48px -12px rgb(0 0 0 / 0.18), 0 4px 12px -4px rgb(0 0 0 / 0.08)",
        subtle: "0 1px 2px 0 rgb(0 0 0 / 0.04)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "overlay-in": {
          from: { opacity: "0", transform: "translateY(4px) scale(0.99)" },
          to: { opacity: "1", transform: "translateY(0) scale(1)" },
        },
      },
      animation: {
        "fade-in": "fade-in 120ms ease-out",
        "overlay-in": "overlay-in 160ms cubic-bezier(0.16, 1, 0.3, 1)",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
