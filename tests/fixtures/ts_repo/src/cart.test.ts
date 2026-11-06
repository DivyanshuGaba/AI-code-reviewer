import Cart from "./cart";

test("total", () => {
  const c = new Cart();
  c.add("a", 10, 1);
  expect(c.total()).toBe(10);
});
