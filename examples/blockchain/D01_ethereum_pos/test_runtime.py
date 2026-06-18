#!/usr/bin/env python3

from __future__ import annotations

from seedemu.testing import EthereumRuntimeTest


def main() -> int:
    test = EthereumRuntimeTest(__file__)

    geth_nodes = test.require_ethereum_services(
        "D01 has a Geth node for the transaction test",
        minimum_count=1,
        class_contains="Ethereum-POS-Geth",
        consensus="POS",
    )

    if geth_nodes:
        test.send_ether_and_verify(
            "A signed ETH transfer is included and changes balances",
            geth_nodes[0],
            value_wei=10**18,
        )

    test.write_summary("d01-ethereum-pos-runtime-test.json")
    return test.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
