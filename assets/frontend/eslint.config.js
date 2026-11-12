import js from "@eslint/js";
import globals from "globals";
import jestDom from "eslint-plugin-jest-dom";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import testingLibrary from "eslint-plugin-testing-library";
import tseslint from "typescript-eslint";

const tsFiles = ["**/*.{ts,tsx}"];
const appFiles = ["src/**/*.{ts,tsx}"];
const testFiles = ["src/**/*.{test,spec}.{ts,tsx}", "src/test/**/*.{ts,tsx}"];

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**", "tests/e2e/**", "eslint.config.js"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: tsFiles,
    languageOptions: {
      ecmaVersion: 2024,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
      globals: {
        ...globals.node,
      },
    },
    rules: {
      "@typescript-eslint/consistent-type-imports": [
        "error",
        { fixStyle: "inline-type-imports", prefer: "type-imports" },
      ],
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-misused-promises": [
        "error",
        { checksVoidReturn: { arguments: false } },
      ],
    },
  },
  {
    ...jsxA11y.flatConfigs.recommended,
    files: appFiles,
    languageOptions: {
      ...jsxA11y.flatConfigs.recommended.languageOptions,
      globals: {
        ...globals.browser,
        ...globals.serviceworker,
      },
      parserOptions: {
        ...jsxA11y.flatConfigs.recommended.languageOptions?.parserOptions,
      },
    },
  },
  {
    files: appFiles,
    languageOptions: {
      ecmaVersion: 2024,
      globals: {
        ...globals.browser,
        ...globals.serviceworker,
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": [
        "error",
        { allowConstantExport: true },
      ],
    },
  },
  {
    ...testingLibrary.configs["flat/react"],
    files: testFiles,
  },
  {
    ...jestDom.configs["flat/recommended"],
    files: testFiles,
  },
  {
    // Tests fail on defects, never on how fast the machine is. See
    // docs/standards/testing-standard.md (speed-independent frontend tests).
    files: testFiles,
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          // A found element and its state must be read together: under load the state can change
          // between `await findBy*` and the matcher. One waitFor retries until both hold.
          selector: "CallExpression[callee.property.name=/^(?!toBeInTheDocument$)(not|to[A-Z]\\w*)$/] > MemberExpression > CallExpression[callee.name='expect'] > AwaitExpression > CallExpression[callee.property.name=/^find(All)?By/]",
          message: "Read the element and its state together: await waitFor(() => expect(screen.getBy…(…)).toHave…(…)). See docs/standards/testing-standard.md (speed-independent frontend tests).",
        },
        {
          selector: "CallExpression[callee.property.name=/^to[A-Z]/][callee.property.name!='toBeInTheDocument'] > MemberExpression[property.name=/^to/] > MemberExpression[property.name='not'] > CallExpression[callee.name='expect'] > AwaitExpression > CallExpression[callee.property.name=/^find(All)?By/]",
          message: "Read the element and its state together: await waitFor(() => expect(screen.getBy…(…)).not.toHave…(…)). See docs/standards/testing-standard.md (speed-independent frontend tests).",
        },
        {
          // A wait ends as soon as its condition holds; its limit is a hang guard from
          // src/test/time-budget.ts, never a hard-coded number tuned to one machine.
          selector: "CallExpression:matches([callee.name=/^(waitFor|waitForElementToBeRemoved|find(All)?By[A-Za-z]+)$/], [callee.property.name=/^(waitFor|waitUntil|waitForElementToBeRemoved|find(All)?By[A-Za-z]+)$/]) > ObjectExpression > Property[key.name='timeout'][value.type='Literal']",
          message: "Do not hard-code a wait limit: drop the timeout (the shared budget applies), or use asyncWaitMs() from @/test/time-budget for vi.waitFor. See docs/standards/testing-standard.md (speed-independent frontend tests).",
        },
      ],
    },
  },
);
