"use strict";

const assert = require("node:assert/strict");
const expand = require("brace-expansion");

assert.equal(typeof expand, "function", "legacy CommonJS consumers require a callable export");
assert.equal(expand.expand, expand, "modern consumers require the named expand export");

const maxLength = 100_000;
const expansions = expand("{a,b}".repeat(1_500), { maxLength });
const totalLength = expansions.reduce((total, value) => total + value.length, 0);

assert.ok(expansions.length > 0, "bounded expansion must preserve legitimate output");
assert.ok(totalLength <= maxLength, "expansion output must remain within the memory bound");
