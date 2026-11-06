import pkg.orders
from pkg import OrderBook, create_order


def main():
    book = OrderBook()
    create_order(book, 10)
    pkg.orders.cancel_order(book, {"amount": 10})


def helper():
    return main()


if __name__ == "__main__":
    main()
