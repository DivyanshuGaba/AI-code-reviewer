const { applyDiscount } = require("../src/pricing");

function discounted(p) {
  return applyDiscount(p, 10);
}

module.exports = { discounted };
