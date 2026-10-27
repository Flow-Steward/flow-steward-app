"use strict";

const safeBraceExpansion = require("brace-expansion-safe");
const { expand } = safeBraceExpansion;

module.exports = expand;
Object.assign(module.exports, safeBraceExpansion);
