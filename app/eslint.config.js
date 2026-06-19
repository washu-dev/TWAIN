const { defineConfig } = require('eslint/config');

module.exports = defineConfig([
  {
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      'no-unused-vars': 'warn',
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      'prefer-const': 'error',
      'no-var': 'error',
    },
  },
]);
