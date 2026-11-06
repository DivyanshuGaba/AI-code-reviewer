import Cart from "./cart";
import { applyDiscount } from "./index";

export function main(): string {
  const cart = new Cart();
  cart.add("widget", 100, 10);
  console.log(applyDiscount(5, 10));
  return cart.label();
}
