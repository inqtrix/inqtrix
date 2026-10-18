import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
    ],
    languageOptions: {
      globals: globals.browser,
    },
  },
  {
    // Design-language guard: feature UI must use the semantic .t-* text roles
    // defined in src/styles/globals.css, never ad-hoc font sizes or line
    // heights, so a text size can only enter the app as a named role.
    // Warn-only so it never blocks the build; it flags new drift in
    // review. Control primitives + the Markdown renderers live outside
    // src/features and keep their own deliberate scales.
    files: ['src/features/**/*.tsx'],
    rules: {
      'no-restricted-syntax': [
        'warn',
        {
          selector: 'Literal[value=/text-\\[\\d+px\\]/]',
          message: 'Use a .t-* typography role class, not an arbitrary text-[..px] size.',
        },
        {
          selector: 'TemplateElement[value.raw=/text-\\[\\d+px\\]/]',
          message: 'Use a .t-* typography role class, not an arbitrary text-[..px] size.',
        },
        {
          selector: 'Literal[value=/leading-\\[/]',
          message: 'Use the line-height bundled in a .t-* typography role class, not an arbitrary leading-[..] value.',
        },
        {
          selector: 'TemplateElement[value.raw=/leading-\\[/]',
          message: 'Use the line-height bundled in a .t-* typography role class, not an arbitrary leading-[..] value.',
        },
      ],
    },
  },
])
