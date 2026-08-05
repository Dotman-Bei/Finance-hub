/** @type {import('tailwindcss').Config} */

// ── Horizon design tokens ───────────────────────────────────────────────────
// Vibrant energy held inside a minimal, near-white, Apple-inspired shell.
// Token names (surface, outline-variant, on-surface…) are the contract the
// components code against — change values here, never in component classes.
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Primary accent — vibrant orange/coral
        primary: {
          DEFAULT: '#FF8A65',
          50: '#FFF4F0',
          100: '#FFE7DE',
          200: '#FFCDBB',
          300: '#FFAE91',
          400: '#FF8A65',
          500: '#FF6E3F',
          600: '#F2551F',
          700: '#C94214',
          800: '#9C3411',
          900: '#7A2B0F',
        },

        // Surfaces — pure white through extremely light grays
        surface: {
          DEFAULT: '#FFFFFF',
          variant: '#FAFAFB',
          dim: '#F4F4F6',
          sunken: '#EFEFF2',
        },

        // Outlines — thin, semi-transparent hairlines
        outline: {
          DEFAULT: '#D5D5DC',
          variant: '#E7E7EC',
        },

        // Type — solid black for headers/logos/copyright (brand contrast rule)
        'on-surface': {
          DEFAULT: '#000000',
          variant: '#6A6A75',
          muted: '#9A9AA5',
        },

        // Dynamic-energy gradient stops
        electric: '#0A84FF',
        magenta: '#E935C1',

        // Reconciliation status semantics
        matched: '#0F9E8E',
        exception: '#F5A524',
        quarantined: '#E5484D',
      },

      fontFamily: {
        sans: [
          '"Plus Jakarta Sans"',
          '-apple-system',
          'BlinkMacSystemFont',
          '"Segoe UI"',
          'Inter',
          'sans-serif',
        ],
        mono: ['"SF Mono"', '"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },

      // Editorial headings: bold + tight tracking. Body: generous leading.
      letterSpacing: {
        tightest: '-0.045em',
        tighter: '-0.032em',
        'tight-ui': '-0.02em',
      },

      fontSize: {
        display: ['clamp(2.75rem, 6vw, 4.5rem)', { lineHeight: '0.95', letterSpacing: '-0.045em', fontWeight: '800' }],
        headline: ['clamp(1.75rem, 3vw, 2.5rem)', { lineHeight: '1.08', letterSpacing: '-0.035em', fontWeight: '700' }],
        title: ['1.125rem', { lineHeight: '1.3', letterSpacing: '-0.02em', fontWeight: '600' }],
        body: ['0.9375rem', { lineHeight: '1.7', fontWeight: '500' }],
        caption: ['0.75rem', { lineHeight: '1.5', letterSpacing: '0.02em', fontWeight: '600' }],
      },

      // Vertical rhythm — 40px is the canonical gap between feature blocks
      spacing: {
        rhythm: '40px',
        'rhythm-2': '80px',
        'rhythm-3': '120px',
      },

      borderRadius: {
        pill: '999px',
        glass: '24px',
        pane: '28px',
      },

      backdropBlur: {
        glass: '20px',
        'glass-xl': '32px',
      },

      boxShadow: {
        // Soft shadows replace stark containers
        glass: '0 1px 2px rgba(16,16,20,0.04), 0 8px 32px -8px rgba(16,16,20,0.10)',
        'glass-lg': '0 2px 4px rgba(16,16,20,0.04), 0 24px 64px -16px rgba(16,16,20,0.16)',
        float: '0 1px 1px rgba(16,16,20,0.04), 0 12px 40px -12px rgba(16,16,20,0.18)',
        'primary-glow': '0 8px 28px -8px rgba(255,138,101,0.55)',
        inset: 'inset 0 1px 0 rgba(255,255,255,0.7)',
      },

      backgroundImage: {
        'grad-energy': 'linear-gradient(120deg, #0A84FF 0%, #7B5BF5 48%, #E935C1 100%)',
        'grad-warm': 'linear-gradient(120deg, #FF8A65 0%, #FF6E3F 100%)',
        'grad-subtle':
          'radial-gradient(60% 80% at 20% 0%, rgba(10,132,255,0.10) 0%, transparent 60%), radial-gradient(50% 70% at 85% 15%, rgba(233,53,193,0.09) 0%, transparent 60%)',
        'glass-sheen':
          'linear-gradient(180deg, rgba(255,255,255,0.85) 0%, rgba(255,255,255,0.45) 100%)',
      },

      keyframes: {
        'fade-up': {
          from: { opacity: '0', transform: 'translateY(14px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        'fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        'slide-in-right': {
          from: { opacity: '0', transform: 'translateX(24px) scale(0.98)' },
          to: { opacity: '1', transform: 'translateX(0) scale(1)' },
        },
        'pulse-ring': {
          '0%': { transform: 'scale(0.85)', opacity: '0.85' },
          '70%': { transform: 'scale(1.8)', opacity: '0' },
          '100%': { transform: 'scale(1.8)', opacity: '0' },
        },
        shimmer: {
          '100%': { transform: 'translateX(100%)' },
        },
        drift: {
          '0%,100%': { transform: 'translate3d(0,0,0) scale(1)' },
          '50%': { transform: 'translate3d(2%, -3%, 0) scale(1.06)' },
        },
      },

      animation: {
        'fade-up': 'fade-up 0.6s cubic-bezier(0.16,1,0.3,1) both',
        'fade-in': 'fade-in 0.5s ease both',
        'slide-in-right': 'slide-in-right 0.45s cubic-bezier(0.16,1,0.3,1) both',
        'pulse-ring': 'pulse-ring 2s cubic-bezier(0.4,0,0.6,1) infinite',
        shimmer: 'shimmer 1.8s infinite',
        drift: 'drift 18s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
