export function applyDiscount(price: number, pct: number): number {
  if (pct < 0 || pct > 100) {
    throw new RangeError("pct must be between 0 and 100");
  }
  return Math.round(price * (1 - pct / 100) * 100) / 100;
}

export const bulkUnitPrice = (unit: number, qty: number): number =>
  qty >= 10 ? applyDiscount(unit, 15) : unit;
