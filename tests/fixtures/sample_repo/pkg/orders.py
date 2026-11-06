import json

from pkg import payments
from pkg.payments import Gateway


class OrderBook:
    def __init__(self):
        self.orders = []

    def add(self, order):
        self.orders.append(order)
        return len(self.orders)

    def total(self):
        return sum(o["amount"] for o in self.orders)


def create_order(book, amount):
    gateway = Gateway()
    gateway.charge(amount)
    order = {"amount": amount}
    book.add(order)
    return json.dumps(order)


def cancel_order(book, order):
    payments.refund(order["amount"])
    return book.total()
