#!/usr/bin/env python3
# encoding: utf-8

import unittest as ut
from .SEEDBlockchain import Wallet
from seedemu import *
import time
import requests
from tests import SeedEmuTestCase

class EthereumPOSTestCase(SeedEmuTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        
        cls.rpc_url = 'http://10.150.0.72:8545'
        cls.beacon_api_url = 'http://10.152.0.71:8000'
        # Target block delta for POS
        cls.wallet1 = Wallet(chain_id=1337)
        for name in ['Alice', 'Bob', 'Charlie', 'David', 'Eve']:
            cls.wallet1.createAccount(name)

        return
    # Test POS geth connection
    def test_pos_geth_connection(self):

        url = self.rpc_url
        i = 1
        current_time = time.time()
        while True:
            self.printLog("\n----------Trial {}----------".format(i))
            if time.time() - current_time > 600:
                self.printLog("TimeExhausted: 600 sec")
            try:
                self.wallet1.connectToBlockchain(url, isPOA=True)
                self.printLog("Connection Succeed: ", url)
                break
            except Exception as e:
                self.printLog(e)
                time.sleep(20)
                i += 1
        self.assertTrue(self.wallet1._web3.isConnected())

    # Test POS beacon connection
    def test_pos_beacon_connection(self):
        base_url = self.beacon_api_url

        i = 1
        current_time = time.time()
        while True:
            self.printLog("\n----------Trial {}----------".format(i))
            if time.time() - current_time > 600:
                self.fail("TimeExhausted: 600 sec")
            try:
                version_url = "{}/eth/v1/node/version".format(base_url)

                version_resp = requests.get(version_url, timeout=5)
                version_data = version_resp.json()
                version_str = str(version_data.get("data", {}).get("version", ""))
                self.assertIn("lighthouse", version_str.lower())
                self.printLog("Beacon API reachable: {}".format(base_url))
                break
            except Exception as e:
                self.printLog(e)
                time.sleep(20)
                i += 1
        return

    def test_block_number_increases(self):
        url = self.rpc_url
        try:
            if (not hasattr(self.wallet1, "_web3")) or (not self.wallet1._web3.isConnected()):
                self.wallet1.connectToBlockchain(url, isPOA=True)
        except Exception as e:
            self.fail("failed to connect execution RPC {}: {}".format(url, e))

        start_bn = self.wallet1._web3.eth.block_number
        #wait for blocknumber increase 5 blocks
        target_bn = start_bn + 5
        self.printLog("Waiting for blocks: {} -> {}".format(start_bn, target_bn))

        start_time = time.time()
        last_bn = start_bn
        while time.time() - start_time < 300:
            time.sleep(6)
            cur_bn = self.wallet1._web3.eth.block_number
            if cur_bn != last_bn:
                self.printLog("block number: {} (+ {})".format(cur_bn, cur_bn - start_bn))
                last_bn = cur_bn
            if cur_bn >= target_bn:
                self.assertGreaterEqual(cur_bn, target_bn)
                return
        self.fail("block number did not reach target delta")

    def test_peer_counts(self):
        peer_counts = len(self.wallet1._web3.geth.admin.peers())
        self.assertEqual(peer_counts, 4)

    def test_validators_status(self):
        base_url = self.beacon_api_url
        url = "{}/eth/v1/beacon/states/head/validators".format(base_url)

        i = 1
        current_time = time.time()
        while True:
            self.printLog("\n----------Trial {}----------".format(i))
            if time.time() - current_time > 600:
                self.fail("TimeExhausted: 600 sec")
            try:
                resp = requests.get(url, params={"status": "active"}, timeout=10)
                self.assertTrue(
                    200 <= resp.status_code < 300,
                    "Validators endpoint failed: {} {}".format(resp.status_code, resp.text),
                )
                payload = resp.json()
                validators = payload.get("data", [])
                self.assertEqual(len(validators), 15)
                balances_1 = {}
                for v in validators:
                    status = str(v.get("status", "")).lower()
                    self.assertTrue(status.startswith("active"))
                    vid = str(v.get("index", ""))
                    bal = v.get("balance", None)
                    self.assertTrue(bal is not None)
                    balances_1[vid] = int(bal)
                self.printLog("Active validators: {}".format(len(validators)))

                time.sleep(12)
                resp2 = requests.get(url, params={"status": "active"}, timeout=10)
                self.assertTrue(
                    200 <= resp2.status_code < 300,
                    "Validators endpoint failed: {} {}".format(resp2.status_code, resp2.text),
                )
                payload2 = resp2.json()
                validators2 = payload2.get("data", [])
                self.assertEqual(len(validators2), 15)
                balances_2 = {}
                for v in validators2:
                    status = str(v.get("status", "")).lower()
                    self.assertTrue(status.startswith("active"))
                    vid = str(v.get("index", ""))
                    bal = v.get("balance", None)
                    self.assertTrue(bal is not None)
                    balances_2[vid] = int(bal)

                changed = False
                for vid, bal1 in balances_1.items():
                    bal2 = balances_2.get(vid)
                    if bal2 is None:
                        continue
                    if bal1 != bal2:
                        changed = True
                        self.printLog("validator balance changed: {} -> {}".format(bal1, bal2))
                        break

                if changed:
                    return
                raise Exception("validator balances did not change yet")
            except Exception as e:
                self.printLog(e)
                time.sleep(20)
                i += 1

    def test_pos_send_transaction(self):
        time.sleep(10)
        balance_rpc_url = "http://10.151.0.72:8545"
        balance_wallet = Wallet(chain_id=1337)
        balance_wallet.connectToBlockchain(balance_rpc_url, isPOA=True)

        recipient = self.wallet1.getAccountAddressByName('Bob')

        recipient_before = balance_wallet._web3.eth.get_balance(recipient)

        value_wei = 100000000000000000
        txhash = self.wallet1.sendTransaction(recipient, 0.1, sender_name='David', wait=True, verbose=False)
        receipt = self.wallet1.getTransactionReceipt(txhash)
        self.assertEqual(receipt.get("status"), 1)

        start_time = time.time()
        while True:
            recipient_after = balance_wallet._web3.eth.get_balance(recipient)
            if recipient_after - recipient_before == value_wei:
                break
            if time.time() - start_time > 60:
                break
            time.sleep(3)

        recipient_after = balance_wallet._web3.eth.get_balance(recipient)

        self.assertEqual(recipient_after - recipient_before, value_wei)

    def  test_beacon_slot_increases(self):
        base_url = self.beacon_api_url
        url = "{}/eth/v1/beacon/headers/head".format(base_url)

        def _get_slot() -> int:
            resp = requests.get(url, timeout=10)
            self.assertTrue(
                200 <= resp.status_code < 300,
                "Beacon headers endpoint failed: {} {}".format(resp.status_code, resp.text),
            )
            payload = resp.json()
            slot_str = (
                payload.get("data", {})
                .get("header", {})
                .get("message", {})
                .get("slot", "0")
            )
            return int(slot_str)

        i = 1
        current_time = time.time()
        while True:
            self.printLog("\n----------Trial {}----------".format(i))
            if time.time() - current_time > 600:
                self.fail("TimeExhausted: 600 sec")
            try:
                start_slot = _get_slot()
                target_slot = start_slot + 5
                self.printLog("Waiting for slot: {} -> {}".format(start_slot, target_slot))

                start_time = time.time()
                last_slot = start_slot
                while time.time() - start_time < 180:
                    time.sleep(6)
                    cur_slot = _get_slot()
                    if cur_slot != last_slot:
                        self.printLog("slot: {} (+ {})".format(cur_slot, cur_slot - start_slot))
                        last_slot = cur_slot
                    if cur_slot >= target_slot:
                        self.assertGreaterEqual(cur_slot, target_slot)
                        return
                self.fail("slot did not increase")
            except Exception as e:
                self.printLog(e)
                time.sleep(20)
                i += 1

    def test_faucet_static_fund(self):
            fund_address = "0x40e38EF94ab2bC9506167D478821ffd55ff2d88d"
            # Set maximum number of attempts
            max_attempts = 10
            attempts = 0

            while attempts < max_attempts:
                if self.wallet1._web3.eth.getBalance(fund_address) >= 2*EthUnit.ETHER.value:
                    break
                else:
                    # Increment the attempts counter
                    attempts += 1
                    # Wait for a short duration before trying again (e.g., 5 seconds)
                    time.sleep(10)
            self.assertTrue(self.wallet1._web3.eth.getBalance(fund_address) >= 2*EthUnit.ETHER.value)

    def test_faucet_dynamic_fund(self):
            max_attempts = 20
            attempts = 0

            while attempts < max_attempts:
                try:
                    # Send the POST request
                    response = requests.get('http://10.164.0.71:80/')
                    # Check if the request was successful (status code 200)
                    if response.status_code == 200:
                        print("POST request successful")
                        break  # Exit the loop if successful
                    else:
                        print(f"POST request failed with status code {response.status_code}")
                except requests.exceptions.RequestException as e:
                    print(f"Error: {e}")
                
                # Increment the attempts counter
                attempts += 1
                # Wait for a short duration before trying again (e.g., 5 seconds)
                time.sleep(10)
            fund_address = "0x9e4f73dE97FEB05FE4e3c0d42B92585C9A0c0E91"

            # Define the URL of faucet API endpoint
            url = 'http://10.164.0.71:80/fundme'
            # Define the parameters to send in the POST request
            params = {'address': fund_address, 'amount': 5}
            # Send the POST request
            response = requests.post(url, data=params)
            print(response)
            while attempts < max_attempts:
                if self.wallet1._web3.eth.getBalance(fund_address) >= 5*EthUnit.ETHER.value:
                    break
                else:
                    # Increment the attempts counter
                    attempts += 1
                    # Wait for a short duration before trying again (e.g., 5 seconds)
                    time.sleep(10)

            self.assertTrue(self.wallet1._web3.eth.getBalance(fund_address) >= 5*EthUnit.ETHER.value)

    def test_beacon_peers(self):
        '''
        Test the connection status of beacon peers; it is considered normal 
        when the number of consensus-layer peers is greater than or equal to 1.
        '''
        base_url = self.beacon_api_url
        url = "{}/eth/v1/node/peers".format(base_url)

        i = 1
        current_time = time.time()
        while True:
            self.printLog("\n----------Trial {}----------".format(i))
            if time.time() - current_time > 600:
                self.fail("TimeExhausted: 600 sec")
            try:
                resp = requests.get(url, timeout=10)
                self.assertTrue(
                    200 <= resp.status_code < 300,
                    "Peers endpoint failed: {} {}".format(resp.status_code, resp.text),
                )
                payload = resp.json()
                peers = payload.get("data", [])
                self.assertGreaterEqual(len(peers), 1)
                connected_peers = 0
                for p in peers:
                    state = str(p.get("state", "")).lower()
                    if state == "connected":
                        connected_peers += 1
                self.assertGreaterEqual(connected_peers, 1)
                self.printLog("Beacon peers: total={}, connected={}".format(len(peers), connected_peers))
                return
            except Exception as e:
                self.printLog(e)
                time.sleep(20)
                i += 1

    def test_utility(self):
       
        max_attempts = 20
        utility_url = "http://10.164.0.72:5000"

        contract_data = {
            "contract_name": "test999",
            "contract_address": "0xc0ffee254729296a45a3885639AC7E10F9d54979"
        }
        

        register_resp = requests.post("{}/register_contract".format(utility_url), json=contract_data, timeout=10)
        self.assertTrue(200 <= register_resp.status_code < 300, "Failed to register contract: {} {}".format(register_resp.status_code, register_resp.text))

        expected_address = contract_data["contract_address"]
        attempts = 0
        while attempts < max_attempts:
            list_resp = requests.get("{}/contracts_info".format(utility_url), timeout=10)
            if 200 <= list_resp.status_code < 300:
                contracts_info = list_resp.json()
                if contracts_info.get("test999") == expected_address:
                    self.printLog("Contract registration and retrieval successful.")
                    return
            time.sleep(3)
            attempts += 1

        self.fail("Registered contract not found in /contracts_info at {}".format(utility_url))

    @classmethod
    def get_test_suite(cls):
        test_suite = ut.TestSuite()
        test_suite.addTest(cls('test_pos_geth_connection'))
        test_suite.addTest(cls('test_pos_beacon_connection'))
        test_suite.addTest(cls('test_validators_status'))   
        test_suite.addTest(cls('test_block_number_increases'))
        test_suite.addTest(cls('test_beacon_slot_increases'))
        test_suite.addTest(cls('test_beacon_peers'))
        test_suite.addTest(cls('test_peer_counts'))
        test_suite.addTest(cls('test_pos_send_transaction'))
        test_suite.addTest(cls('test_faucet_static_fund'))
        test_suite.addTest(cls('test_faucet_dynamic_fund'))
        test_suite.addTest(cls('test_utility'))
        return test_suite
if __name__ == "__main__":
        test_suite = EthereumPOSTestCase.get_test_suite()
        res = ut.TextTestRunner(verbosity=2).run(test_suite)
    
        EthereumPOSTestCase.printLog("----------Test %d--------=")
        num, errs, fails = res.testsRun, len(res.errors), len(res.failures)
        EthereumPOSTestCase.printLog("score: %d of %d (%d errors, %d failures)" % (num - (errs+fails), num, errs, fails))
        
