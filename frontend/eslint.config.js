import js from '@eslint/js'
import globals from 'globals'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

// Correctness only, no stylistic rules. The codebase already has a consistent
// house style (no semicolons, single quotes) that nothing here enforces or
// fights -- a linter that reformats on install produces a diff nobody reviews.
//
// The rule that earns its keep is react-hooks/exhaustive-deps: useWebSocket.js
// keeps a callback in a ref precisely to avoid a stale-closure reconnect loop,
// and that invariant is invisible to review but visible to this rule.
export default [
  { ignores: ['dist/**', 'node_modules/**'] },

  js.configs.recommended,
  react.configs.flat.recommended,
  react.configs.flat['jsx-runtime'], // Vite's automatic runtime: no `import React`

  {
    files: ['**/*.{js,jsx}'],

    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.es2021 },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },

    settings: { react: { version: 'detect' } },

    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },

    rules: {
      ...reactHooks.configs.recommended.rules,

      // Fast Refresh only works when a module exports components and nothing
      // else. constants.js / format.js are plain modules, hence allowConstantExport.
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],

      // No prop-types in this codebase by choice -- the API shapes are
      // normalised once in api/reportingApi.js rather than re-declared per
      // component.
      'react/prop-types': 'off',

      // Caught unused vars are deliberate: `catch {}` is used for JSON.parse
      // and WebSocket construction where the error carries no information.
      'no-unused-vars': ['error', { argsIgnorePattern: '^_', caughtErrors: 'none' }],
    },
  },

  // Config files run in Node, not the browser.
  {
    files: ['*.config.js', 'postcss.config.js', 'tailwind.config.js'],
    languageOptions: { globals: { ...globals.node } },
  },
]
