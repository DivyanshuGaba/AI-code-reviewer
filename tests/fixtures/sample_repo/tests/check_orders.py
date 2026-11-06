from pkg.payments import Gateway


def test_charge_rounds():
    gateway = Gateway()
    assert gateway.charge(1.239) == 1.24
