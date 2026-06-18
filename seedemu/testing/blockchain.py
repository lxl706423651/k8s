#!/usr/bin/env python3

from __future__ import annotations

import json
import re
import shlex
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .base import TestRunner, TestRunnerError
    from .runtime import ADDRESS_LABEL, ComposeRuntimeTest, ComposeService
except ImportError:
    from base import TestRunner, TestRunnerError
    from runtime import ADDRESS_LABEL, ComposeRuntimeTest, ComposeService


META_PREFIX = "org.seedsecuritylabs.seedemu.meta."
CLASS_LABEL = META_PREFIX + "class"
DISPLAY_LABEL = META_PREFIX + "displayname"
ETH_NODE_ID_LABEL = META_PREFIX + "ethereum.node_id"
ETH_ROLE_LABEL = META_PREFIX + "ethereum.role"
ETH_CONSENSUS_LABEL = META_PREFIX + "ethereum.consensus"
ETH_CHAIN_ID_LABEL = META_PREFIX + "ethereum.chain_id"

DEFAULT_TRANSFER_RECIPIENT = "0x1000000000000000000000000000000000000001"
LOCAL_ACCOUNT_CACHE_VERSION = 1


def _label_list(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    text = str(value)
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return [text]
    if isinstance(decoded, list):
        return [str(item) for item in decoded]
    return [str(decoded)]


def _ethereum_service_matches(
    labels: Dict[str, object],
    *,
    class_contains: Optional[str] = None,
    display_contains: Optional[str] = None,
    role: Optional[str] = None,
    consensus: Optional[str] = None,
) -> bool:
    if class_contains and class_contains not in str(labels.get(CLASS_LABEL, "")):
        return False
    if display_contains and display_contains not in str(labels.get(DISPLAY_LABEL, "")):
        return False
    if role and role not in _label_list(labels.get(ETH_ROLE_LABEL)):
        return False
    if consensus and str(labels.get(ETH_CONSENSUS_LABEL, "")).upper() != consensus.upper():
        return False
    if ETH_NODE_ID_LABEL not in labels and not class_contains and not display_contains:
        return False
    return True


def _matching_ethereum_services(
    compose: Dict[str, object],
    *,
    class_contains: Optional[str] = None,
    display_contains: Optional[str] = None,
    role: Optional[str] = None,
    consensus: Optional[str] = None,
) -> List[ComposeService]:
    services: List[ComposeService] = []
    for name, service in compose.get("services", {}).items():
        labels = service.get("labels", {})
        if not isinstance(labels, dict):
            continue
        if not _ethereum_service_matches(
            labels,
            class_contains=class_contains,
            display_contains=display_contains,
            role=role,
            consensus=consensus,
        ):
            continue
        address = str(labels.get(ADDRESS_LABEL, "")).split("/", 1)[0]
        services.append(ComposeService(name=str(name), address=address, labels=dict(labels)))
    return sorted(services, key=lambda item: item.name)


class BlockchainTestRunner(TestRunner):
    """Runner for blockchain emulation tests."""

    runner_name = "blockchain"
    probe_handlers = {
        **TestRunner.probe_handlers,
        "json-rpc": "probe_json_rpc",
        "ethereum-rpc": "probe_json_rpc",
        "blockchain-rpc": "probe_json_rpc",
        "ethereum-service-count": "probe_ethereum_service_count",
        "ethereum-compose-ps": "probe_ethereum_compose_ps",
        "ethereum-exec": "probe_ethereum_exec",
        "ethereum-block-progress": "probe_ethereum_block_progress",
    }

    def probe_json_rpc(self, probe: Dict[str, Any]) -> tuple[bool, str]:
        payload = {
            "jsonrpc": "2.0",
            "id": probe.get("id", 1),
            "method": probe["method"],
            "params": probe.get("params", []),
        }
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            str(probe["url"]),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=int(probe.get("timeout", 10))) as response:
                text = response.read().decode("utf-8", errors="replace")
                if response.status != int(probe.get("expect_status", 200)):
                    return False, "HTTP {}, expected {}".format(response.status, probe.get("expect_status", 200))
                data = json.loads(text)
        except Exception as exc:
            return False, str(exc)

        if "expect_result" in probe and data.get("result") != probe["expect_result"]:
            return False, "JSON-RPC result did not match"
        if "expect_result_contains" in probe and str(probe["expect_result_contains"]) not in str(data.get("result")):
            return False, "JSON-RPC result did not contain expected text"
        if "expect_error" in probe and data.get("error") != probe["expect_error"]:
            return False, "JSON-RPC error did not match"
        if probe.get("expect_no_error", True) and data.get("error") is not None:
            return False, "JSON-RPC returned error {}".format(data.get("error"))
        return True, "JSON-RPC response matched"

    def required_probe_fields(self, probe_type: str) -> Optional[List[str]]:
        fields = {
            "json-rpc": ["url", "method"],
            "ethereum-rpc": ["url", "method"],
            "blockchain-rpc": ["url", "method"],
            "ethereum-service-count": [],
            "ethereum-compose-ps": [],
            "ethereum-exec": ["command"],
            "ethereum-block-progress": [],
        }.get(probe_type)
        return fields if fields is not None else super().required_probe_fields(probe_type)

    def probe_ethereum_service_count(self, probe: Dict[str, Any]) -> tuple[bool, str]:
        services = self.ethereum_services_from_probe(probe)
        return self.check_service_count(probe, services)

    def probe_ethereum_compose_ps(self, probe: Dict[str, Any]) -> tuple[bool, str]:
        services = self.ethereum_services_from_probe(probe)
        count_ok, count_message = self.check_service_count(probe, services)
        if not count_ok:
            return False, count_message

        compose = self.compose_file()
        result = self.run_command(
            "probe-{}".format(self.slug(probe["name"])),
            ["docker", "compose", "-f", str(compose), "ps", "--format", "json"],
            cwd=compose.parent,
            env=self.docker_env(),
            timeout=int(probe.get("timeout", 30)),
        )
        if result.returncode != 0:
            return False, "docker compose ps exited {}".format(result.returncode)

        expected = {service.name for service in services}
        running = set()
        observed = []
        for item in self.parse_compose_ps_output(result.stdout or ""):
            name = str(item.get("Service") or item.get("Name"))
            state = str(item.get("State") or item.get("Status"))
            observed.append("{}={}".format(name, state))
            state_text = state.lower()
            if name in expected and (state_text.startswith("running") or state_text.startswith("up")):
                running.add(name)

        missing = sorted(expected - running)
        if missing:
            return False, "services not running: {}; observed: {}".format(
                ", ".join(missing),
                ", ".join(observed) or "<none>",
            )
        return True, "{} matched service(s) running".format(len(services))

    def probe_ethereum_exec(self, probe: Dict[str, Any]) -> tuple[bool, str]:
        service = self.ethereum_service_from_probe(probe)
        if service is None:
            return False, "no matching Ethereum service"
        compose = self.compose_file()
        result = self.run_command(
            "probe-{}".format(self.slug(probe["name"])),
            [
                "docker",
                "compose",
                "-f",
                str(compose),
                "exec",
                "-T",
                service.name,
                "sh",
                "-lc",
                str(probe["command"]),
            ],
            cwd=compose.parent,
            env=self.docker_env(),
            timeout=int(probe.get("timeout", 45)),
        )
        expected = int(probe.get("expect_exit", 0))
        if result.returncode != expected:
            return False, "{} exited {}, expected {}".format(service.name, result.returncode, expected)
        matched, message = self.check_text_expectations(probe, result.stdout or "", result.stderr or "")
        if not matched:
            return False, "{}: {}".format(service.name, message)
        return True, "{}: {}".format(service.name, message)

    def probe_ethereum_block_progress(self, probe: Dict[str, Any]) -> tuple[bool, str]:
        service = self.ethereum_service_from_probe(probe)
        if service is None:
            return False, "no matching Ethereum service"

        start_block: Optional[int] = None
        last_block: Optional[int] = None
        last_error = ""
        min_delta = int(probe.get("min_delta", 1))
        retries = int(probe.get("block_retries", probe.get("retries", 60)))
        interval = float(probe.get("block_interval", probe.get("interval", 5)))
        for attempt in range(1, retries + 1):
            try:
                block = self.get_ethereum_block_number(service, int(probe.get("timeout", 60)))
            except Exception as exc:
                last_error = str(exc)
            else:
                if start_block is None:
                    start_block = block
                last_block = block
                if block >= start_block + min_delta:
                    return True, "{} advanced from {} to {} in {} attempt(s)".format(
                        service.name,
                        start_block,
                        block,
                        attempt,
                    )
            if attempt < retries:
                time.sleep(interval)
        return False, "{} did not advance by {}; start={}, latest={}, error={}".format(
            service.name,
            min_delta,
            start_block,
            last_block,
            last_error,
        )

    def ethereum_services_from_probe(self, probe: Dict[str, Any]) -> List[ComposeService]:
        return _matching_ethereum_services(
            self.load_compose_file(),
            class_contains=probe.get("class_contains"),
            display_contains=probe.get("display_contains"),
            role=probe.get("role"),
            consensus=probe.get("consensus"),
        )

    def ethereum_service_from_probe(self, probe: Dict[str, Any]) -> Optional[ComposeService]:
        services = self.ethereum_services_from_probe(probe)
        if not services:
            return None
        index = int(probe.get("match_index", 0))
        if index < 0 or index >= len(services):
            raise TestRunnerError("probe {} match_index out of range".format(probe["name"]))
        return services[index]

    def check_service_count(self, probe: Dict[str, Any], services: List[ComposeService]) -> tuple[bool, str]:
        count = len(services)
        expected = probe.get("expected_count")
        if expected is not None and count != int(expected):
            return False, "found {} service(s), expected {}; services={}".format(
                count,
                expected,
                ", ".join(service.name for service in services) or "<none>",
            )
        minimum = int(probe.get("minimum_count", 1))
        if expected is None and count < minimum:
            return False, "found {} service(s), expected at least {}".format(count, minimum)
        return True, "found {} service(s): {}".format(
            count,
            ", ".join(service.name for service in services) or "<none>",
        )

    def get_ethereum_block_number(self, service: ComposeService, timeout: int) -> int:
        compose = self.compose_file()
        result = self.run_command(
            "probe-{}-block-number".format(service.name),
            [
                "docker",
                "compose",
                "-f",
                str(compose),
                "exec",
                "-T",
                service.name,
                "sh",
                "-lc",
                "geth attach --exec 'eth.blockNumber'",
            ],
            cwd=compose.parent,
            env=self.docker_env(),
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or "geth attach failed")
        matches = re.findall(r"\b\d+\b", result.stdout or "")
        if not matches:
            raise RuntimeError("could not parse eth.blockNumber from {}".format(result.stdout))
        return int(matches[-1])

    def load_compose_file(self) -> Dict[str, object]:
        try:
            import yaml
        except ImportError as exc:
            raise TestRunnerError("PyYAML is required to read Docker Compose files") from exc
        with self.compose_file().open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)


class EthereumRuntimeTest(ComposeRuntimeTest):
    """Runtime helpers for Ethereum examples.

    These helpers intentionally execute from inside generated containers. That
    keeps tests independent from host port publishing and from host-side web3
    packages.
    """

    def ethereum_services(
        self,
        *,
        class_contains: Optional[str] = None,
        display_contains: Optional[str] = None,
        role: Optional[str] = None,
        consensus: Optional[str] = None,
    ) -> List[ComposeService]:
        return _matching_ethereum_services(
            self.compose,
            class_contains=class_contains,
            display_contains=display_contains,
            role=role,
            consensus=consensus,
        )

    def require_ethereum_services(
        self,
        name: str,
        *,
        expected_count: Optional[int] = None,
        minimum_count: int = 1,
        class_contains: Optional[str] = None,
        display_contains: Optional[str] = None,
        role: Optional[str] = None,
        consensus: Optional[str] = None,
    ) -> List[ComposeService]:
        services = self.ethereum_services(
            class_contains=class_contains,
            display_contains=display_contains,
            role=role,
            consensus=consensus,
        )
        count = len(services)
        if expected_count is None:
            passed = count >= minimum_count
            expectation = "at least {}".format(minimum_count)
        else:
            passed = count == expected_count
            expectation = "exactly {}".format(expected_count)
        self.structural_check(
            name,
            passed,
            "found {} service(s), expected {}; services={}".format(
                count,
                expectation,
                ", ".join(service.name for service in services) or "<none>",
            ),
        )
        return services

    def geth_eval(self, service: ComposeService | str, expression: str, timeout: int = 60) -> Dict[str, object]:
        return self.exec(service, "geth attach --exec {}".format(shlex.quote(expression)), timeout=timeout)

    def ethereum_block_number(self, service: ComposeService | str, timeout: int = 60) -> int:
        result = self.geth_eval(service, "eth.blockNumber", timeout=timeout)
        if result["exit"] != 0:
            raise RuntimeError(result["stderr"] or result["stdout"] or "geth attach failed")
        matches = re.findall(r"\b\d+\b", str(result["stdout"]))
        if not matches:
            raise RuntimeError("could not parse eth.blockNumber from {}".format(result["stdout"]))
        return int(matches[-1])

    def wait_for_ethereum_block_progress(
        self,
        name: str,
        service: ComposeService | str,
        *,
        min_delta: int = 1,
        retries: int = 60,
        interval: int = 5,
        timeout: int = 60,
    ) -> Dict[str, object]:
        start_block: Optional[int] = None
        last_block: Optional[int] = None
        error = ""
        for attempt in range(1, retries + 1):
            try:
                block = self.ethereum_block_number(service, timeout=timeout)
            except Exception as exc:
                error = str(exc)
            else:
                if start_block is None:
                    start_block = block
                last_block = block
                if block >= start_block + min_delta:
                    return self._record_runtime_result(
                        name,
                        service,
                        "wait for eth.blockNumber to advance",
                        0,
                        "start_block={} latest_block={} attempts={}".format(start_block, block, attempt),
                        "",
                    )
            if attempt < retries:
                time.sleep(interval)

        return self._record_runtime_result(
            name,
            service,
            "wait for eth.blockNumber to advance",
            1,
            "start_block={} latest_block={}".format(start_block, last_block),
            error,
        )

    def send_ether_and_verify(
        self,
        name: str,
        service: ComposeService | str,
        *,
        to_address: str = DEFAULT_TRANSFER_RECIPIENT,
        value_wei: int = 10**18,
        password: str = "admin",
        receipt_retries: int = 90,
        timeout: int = 180,
    ) -> Dict[str, object]:
        try:
            script = self._signed_raw_transfer_script(
                service,
                to_address=to_address,
                value_wei=value_wei,
                password=password,
                receipt_retries=receipt_retries,
            )
        except Exception as exc:
            return self._record_runtime_result(name, service, "prepare signed Ethereum transaction", 1, "", str(exc))

        result = self.geth_eval(service, script, timeout=timeout)
        stdout = str(result["stdout"])
        stderr = str(result["stderr"])
        if result["exit"] != 0:
            return self._record_runtime_result(name, service, "send Ethereum transaction", 1, stdout, stderr)

        try:
            data = self._extract_json(stdout)
            self._assert_transfer(data, value_wei)
        except Exception as exc:
            return self._record_runtime_result(name, service, "send Ethereum transaction", 1, stdout, str(exc))

        summary = {
            "sender": data["sender"],
            "recipient": data["recipient"],
            "tx_hash": data["txHash"],
            "block_number": data["receiptBlockNumber"],
            "sender_delta_wei": data["senderDelta"],
            "recipient_delta_wei": data["recipientDelta"],
            "value_wei": str(value_wei),
        }
        return self._record_runtime_result(
            name,
            service,
            "send Ethereum transaction",
            0,
            json.dumps(summary, sort_keys=True),
            "",
        )

    def _record_runtime_result(
        self,
        name: str,
        service: ComposeService | str,
        command: str,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> Dict[str, object]:
        service_name = service.name if isinstance(service, ComposeService) else str(service)
        result = {
            "name": name,
            "service": service_name,
            "command": command,
            "exit": exit_code,
            "stdout": stdout[-1000:],
            "stderr": stderr[-1000:],
            "status": "passed" if exit_code == 0 else "failed",
        }
        self.results.append(result)
        return result

    @staticmethod
    def _extract_json(text: str) -> Dict[str, Any]:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            candidates = [line]
            if line.startswith('"') and line.endswith('"'):
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, str):
                    candidates.insert(0, decoded)
            for candidate in candidates:
                candidate = candidate.strip()
                if candidate.startswith("{") and candidate.endswith("}"):
                    data = json.loads(candidate)
                    if isinstance(data, dict):
                        return data
        raise ValueError("could not find JSON object in geth output")

    @staticmethod
    def _assert_transfer(data: Dict[str, Any], value_wei: int) -> None:
        if not data.get("txHash"):
            raise ValueError("missing transaction hash")
        if data.get("receiptBlockNumber") in (None, ""):
            raise ValueError("transaction was not included in a block")
        status = data.get("receiptStatus")
        if status not in ("0x1", "1", 1, True, None):
            raise ValueError("transaction receipt status is {}".format(status))
        recipient_delta = int(data["recipientDelta"])
        sender_delta = int(data["senderDelta"])
        if recipient_delta != value_wei:
            raise ValueError("recipient delta {} != {}".format(recipient_delta, value_wei))
        if sender_delta < value_wei:
            raise ValueError("sender delta {} is smaller than transfer value {}".format(sender_delta, value_wei))

    def _signed_raw_transfer_script(
        self,
        service: ComposeService | str,
        *,
        to_address: str,
        value_wei: int,
        password: str,
        receipt_retries: int,
    ) -> str:
        try:
            from eth_account import Account
        except ImportError as exc:
            raise RuntimeError("eth_account is required to sign Ethereum runtime test transactions") from exc

        state = self._local_account_transaction_state(service)
        sender = str(state["sender"])
        chain_id = self._chain_id_for_service(service)
        private_key = self._private_key_for_local_account(service, sender, password, chain_id=chain_id)
        account = Account.from_key(private_key)
        if account.address.lower() != sender.lower():
            raise RuntimeError("decrypted key {} does not match local geth account {}".format(account.address, sender))

        transaction = {
            "chainId": chain_id,
            "nonce": self._rpc_int(state["nonce"]),
            "gas": 21000,
            "gasPrice": self._rpc_int(state["gasPrice"]),
            "to": to_address,
            "value": int(value_wei),
            "data": b"",
        }
        signed = Account.sign_transaction(transaction, private_key)
        raw_transaction = getattr(signed, "rawTransaction", None)
        if raw_transaction is None:
            raw_transaction = getattr(signed, "raw_transaction")
        raw_transaction_hex = raw_transaction.hex()
        if not raw_transaction_hex.startswith("0x"):
            raw_transaction_hex = "0x" + raw_transaction_hex

        return self._send_raw_transfer_script(sender, to_address, raw_transaction_hex, receipt_retries)

    def _local_account_transaction_state(self, service: ComposeService | str) -> Dict[str, Any]:
        result = self.geth_eval(service, self._transaction_state_script())
        if result["exit"] != 0:
            raise RuntimeError(result["stderr"] or result["stdout"] or "failed to inspect local geth account")
        return self._extract_json(str(result["stdout"]))

    def _private_key_for_local_account(
        self,
        service: ComposeService | str,
        sender: str,
        password: str,
        *,
        chain_id: Optional[int] = None,
    ) -> str:
        try:
            from eth_account import Account
        except ImportError as exc:
            raise RuntimeError("eth_account is required to decrypt geth keystore files") from exc

        cached_private_key = self._load_cached_local_private_key(sender, chain_id)
        if cached_private_key:
            try:
                cached_account = Account.from_key(cached_private_key)
            except Exception:
                cached_account = None
            if cached_account is not None and cached_account.address.lower() == sender.lower():
                return cached_private_key

        passwords = [password]
        password_result = self.exec(service, "cat /tmp/eth-password 2>/dev/null || true", timeout=30)
        for item in str(password_result["stdout"]).splitlines():
            candidate = item.strip()
            if candidate and candidate not in passwords:
                passwords.append(candidate)

        paths = self._local_keystore_paths(service)
        errors: List[str] = []
        for path_item in paths:
            result = self.exec(service, "cat {}".format(shlex.quote(path_item)), timeout=30)
            if result["exit"] != 0:
                errors.append("{}: {}".format(path_item, result["stderr"] or result["stdout"]))
                continue
            try:
                keyfile = json.loads(str(result["stdout"]))
            except json.JSONDecodeError as exc:
                errors.append("{}: invalid keystore JSON: {}".format(path_item, exc))
                continue
            for candidate_password in passwords:
                try:
                    key = Account.decrypt(keyfile, candidate_password)
                    private_key = key.hex()
                    if not private_key.startswith("0x"):
                        private_key = "0x" + private_key
                    account = Account.from_key(private_key)
                except Exception as exc:
                    errors.append("{}: decrypt failed: {}".format(path_item, exc))
                    continue
                if account.address.lower() == sender.lower():
                    self._save_cached_local_private_key(sender, chain_id, private_key)
                    return private_key

        detail = "; ".join(errors[-3:]) if errors else "no readable keystore files"
        raise RuntimeError("could not decrypt local geth account {}: {}".format(sender, detail))

    def _local_account_cache_path(self) -> Optional[Path]:
        if self.artifact_dir is None:
            return None
        return self.artifact_dir / "ethereum-local-account-cache.json"

    @staticmethod
    def _local_account_cache_key(sender: str, chain_id: Optional[int]) -> str:
        return "{}:{}".format(chain_id if chain_id is not None else "unknown", sender.lower())

    def _load_cached_local_private_key(self, sender: str, chain_id: Optional[int]) -> Optional[str]:
        cache_path = self._local_account_cache_path()
        if cache_path is None or not cache_path.is_file():
            return None
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("version") != LOCAL_ACCOUNT_CACHE_VERSION:
            return None
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            return None
        entry = accounts.get(self._local_account_cache_key(sender, chain_id))
        if not isinstance(entry, dict):
            return None
        private_key = entry.get("private_key")
        if not isinstance(private_key, str) or not private_key:
            return None
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        return private_key

    def _save_cached_local_private_key(self, sender: str, chain_id: Optional[int], private_key: str) -> None:
        cache_path = self._local_account_cache_path()
        if cache_path is None:
            return
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            if cache_path.is_file():
                data = json.loads(cache_path.read_text(encoding="utf-8"))
            else:
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict) or data.get("version") != LOCAL_ACCOUNT_CACHE_VERSION:
            data = {"version": LOCAL_ACCOUNT_CACHE_VERSION, "accounts": {}}
        accounts = data.setdefault("accounts", {})
        if not isinstance(accounts, dict):
            accounts = {}
            data["accounts"] = accounts
        accounts[self._local_account_cache_key(sender, chain_id)] = {
            "sender": sender,
            "chain_id": chain_id,
            "private_key": private_key,
        }
        tmp_path = cache_path.with_name(cache_path.name + ".tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp_path.chmod(0o600)
            tmp_path.replace(cache_path)
            cache_path.chmod(0o600)
        except OSError:
            return

    def _local_keystore_paths(self, service: ComposeService | str) -> List[str]:
        command = (
            'for dir in /root/.ethereum/keystore /tmp/keystore; do '
            '[ -d "$dir" ] && find "$dir" -maxdepth 1 -type f -print; '
            "done | sort -u"
        )
        result = self.exec(service, command, timeout=30)
        if result["exit"] != 0:
            raise RuntimeError(result["stderr"] or result["stdout"] or "failed to list geth keystore files")
        paths = [line.strip() for line in str(result["stdout"]).splitlines() if line.strip()]
        if not paths:
            raise RuntimeError("no geth keystore files found in local account directories")
        return paths

    def _chain_id_for_service(self, service: ComposeService | str) -> int:
        if isinstance(service, ComposeService):
            chain_id = service.labels.get(ETH_CHAIN_ID_LABEL)
            if chain_id is not None:
                return self._rpc_int(chain_id)

        result = self.geth_eval(
            service,
            "JSON.stringify({chainId: admin.nodeInfo.protocols.eth.config.chainId})",
            timeout=30,
        )
        if result["exit"] != 0:
            raise RuntimeError(result["stderr"] or result["stdout"] or "failed to inspect Ethereum chain id")
        return self._rpc_int(self._extract_json(str(result["stdout"]))["chainId"])

    @staticmethod
    def _rpc_int(value: object) -> int:
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if text.startswith(("0x", "0X")):
            return int(text, 16)
        return int(text)

    @staticmethod
    def _transaction_state_script() -> str:
        return """
var sender = eth.accounts[0];
if (!sender) { throw new Error("no local geth account"); }
var nonce = eth.getTransactionCount(sender);
var gasPrice = eth.gasPrice;
JSON.stringify({
  sender: sender,
  nonce: nonce && nonce.toString ? nonce.toString(10) : String(nonce),
  gasPrice: gasPrice && gasPrice.toString ? gasPrice.toString(10) : String(gasPrice)
})
""".strip()

    @staticmethod
    def _send_raw_transfer_script(sender: str, to_address: str, raw_transaction: str, receipt_retries: int) -> str:
        script = """
var sender = __SENDER__;
var recipient = __RECIPIENT__;
var senderBefore = eth.getBalance(sender);
var recipientBefore = eth.getBalance(recipient);
var txHash = eth.sendRawTransaction(__RAW_TRANSACTION__);
var receipt = null;
for (var i = 0; i < __RECEIPT_RETRIES__; i++) {
  receipt = eth.getTransactionReceipt(txHash);
  if (receipt !== null && receipt.blockNumber !== null) { break; }
  admin.sleep(1);
}
var senderAfter = eth.getBalance(sender);
var recipientAfter = eth.getBalance(recipient);
JSON.stringify({
  sender: sender,
  recipient: recipient,
  txHash: txHash,
  receiptBlockNumber: receipt === null ? null : receipt.blockNumber.toString(10),
  receiptStatus: receipt === null || receipt.status === null ? null : receipt.status.toString(10),
  senderBefore: senderBefore.toString(10),
  senderAfter: senderAfter.toString(10),
  recipientBefore: recipientBefore.toString(10),
  recipientAfter: recipientAfter.toString(10),
  senderDelta: senderBefore.minus(senderAfter).toString(10),
  recipientDelta: recipientAfter.minus(recipientBefore).toString(10)
})
""".strip()
        script = script.replace("__SENDER__", json.dumps(sender))
        script = script.replace("__RECIPIENT__", json.dumps(to_address))
        script = script.replace("__RAW_TRANSACTION__", json.dumps(raw_transaction))
        script = script.replace("__RECEIPT_RETRIES__", str(receipt_retries))
        return script
