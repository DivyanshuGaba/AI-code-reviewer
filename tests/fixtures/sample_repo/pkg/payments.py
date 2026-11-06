from .utils import log, round_money


class Gateway:
    def charge(self, amount):
        amount = round_money(amount)
        self._audit(amount)
        return amount

    def _audit(self, amount):
        log(f"charged {amount}")


def refund(amount):
    log("refund")
    return round_money(amount)
