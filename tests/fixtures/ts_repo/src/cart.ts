import { bulkUnitPrice } from "./pricing";
import * as fmt from "./format";

interface Line {
  name: string;
  unit: number;
  qty: number;
}

export default class Cart {
  private lines: Line[] = [];

  add(name: string, unit: number, qty = 1): void {
    this.lines.push({ name, unit, qty });
  }

  total(): number {
    return this.lines.reduce((sum, l) => sum + bulkUnitPrice(l.unit, l.qty) * l.qty, 0);
  }

  label(): string {
    return fmt.money(this.total());
  }
}
