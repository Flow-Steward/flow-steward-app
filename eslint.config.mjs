export default [
  {
    ignores: [
      "**/node_modules/**",
      "**/.git/**",
      "**/docs/**",
    ],
  },
  {
    files: ["assets/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        window: "readonly",
        document: "readonly",
        console: "readonly",
        fetch: "readonly",
        URL: "readonly",
        location: "readonly",
        alert: "readonly",
        prompt: "readonly",
        ELK: "readonly",
        customElements: "readonly",
        HTMLElement: "readonly",
        CSSStyleSheet: "readonly",
        CustomEvent: "readonly",
        FormData: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        localStorage: "readonly",
        sessionStorage: "readonly",
      },
    },
    rules: {
      "no-unused-vars": ["error", { "argsIgnorePattern": "^_" }],
      "no-undef": "error",
      eqeqeq: ["warn", "always"],
      curly: ["warn", "all"],
    },
  },
  {
    files: [
      "assets/platform-kit/**/*.js",
      "assets/platform-renderer.js",
      "assets/platform-renderer-validation.js",
    ],
    languageOptions: {
      sourceType: "module",
    },
  },
];
