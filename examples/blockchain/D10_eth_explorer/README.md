# D10 Eth Explorer Example

This example demonstrates how to build an Ethereum PoS private network in SEED Emulator and automatically attach a blockchain explorer visualization component to it. The visualization is adapted from the open-source projects [eth2-beaconchain-explorer](https://github.com/gobitfly/eth2-beaconchain-explorer.git) and [beaconchain](https://github.com/gobitfly/beaconchain.git). The related Docker build files are located in [`docker_images/seedemu-ethexplorer`](../../../docker_images/seedemu-ethexplorer).

Unlike the Internet Map, which focuses on containers, ASes, network links, and topology, the Eth Explorer connects to the Geth execution-layer node and the Beacon consensus-layer node inside the emulated network. It indexes on-chain data and presents blocks, epochs, slots, validators, and execution-layer transactions through a web UI. With both visualizations enabled, the example lets users inspect both the emulated Internet topology and the Ethereum PoS chain state.

## Example Goals

`ethereum_pos.py` performs the following tasks:

- Creates a base Internet topology with 10 stub ASes: `150-154` and `160-164`.
- Creates an Ethereum PoS node set based on the command-line argument.
- Creates Geth nodes, Beacon nodes, and Validator Client nodes for the PoS network.
- Creates a `BeaconSetupNode` to generate Beacon chain configuration, validator keys, genesis data, and other bootstrapping materials.
- Configures bootnodes for both the execution layer and the consensus layer.
- Enables HTTP RPC on Geth nodes so the explorer backend can read execution-layer data.
- Enables both `internetMapEnabled=True` and `etherViewEnabled=True` when compiling the Docker output.

The first command-line argument controls the node scale. If the argument is `N`, the script creates:

| Type | Count | Purpose |
| --- | --- | --- |
| Geth nodes | `N` | Ethereum execution-layer nodes that provide blocks, transactions, account state, and JSON-RPC APIs |
| Beacon nodes | `N` | Ethereum consensus-layer nodes that maintain Beacon Chain state, slots, epochs, attestations, and related consensus data |
| Validator Client nodes | `3 * N` | Validator clients that connect to Beacon nodes and participate in PoS validation |
| BeaconSetupNode | `1` | Generates and distributes configuration and keys required to start the PoS private chain |

The script automatically calculates how many hosts each stub AS needs and binds the virtual nodes to those hosts. To support larger deployments, it also expands the host IP range of `net0` in each stub AS.

## How to Run

Run the following commands from the project root:

```bash
cd examples/blockchain/D10_eth_explorer
python ethereum_pos.py <beacon_node_count> [amd|arm]
```

Example:

```bash
python ethereum_pos.py 3 amd
```

Arguments:

- `<beacon_node_count>`: The number of Beacon/Geth nodes to create. This also affects the number of Validator Client nodes.
- `amd`: Generate Docker configuration for AMD64. This is the default.
- `arm`: Generate Docker configuration for ARM64 hosts.

After the script finishes, it generates an `output` directory in the current example directory. Start the emulation from that directory:

```bash
cd output
docker compose up
```
When the simulator generates the docker-compose.yml, it will select a Beacon node (`CL_HOST`) by filtering based on the container name containing `"-Beacon-"`, and select a Geth node (`EL_HOST`) by filtering based on `"-Geth-"`.
Startup may take some time. The explorer backend waits for the databases, ClickHouse, Redis, Bigtable emulator services, and Geth/Beacon nodes to become available. It then initializes schemas, reads the chain genesis information, and starts indexing data.

## Access Points

The example compiles the Docker output with:

```python
docker = Docker(internetMapEnabled=True, etherViewEnabled=True, platform=platform)
```

After startup, two main visualization entry points are available:

| Service | Default URL | Function |
| --- | --- | --- |
| Internet Map | `http://127.0.0.1:8080/map.html` | Shows the SEED Emulator network topology, ASes, hosts, containers, and links |
| Eth Explorer | `http://127.0.0.1:5000` | Shows the Ethereum PoS private chain through a blockchain explorer UI |

If a local port is already in use, adjust the port through the `Docker` constructor in `ethereum_pos.py`, for example with `internetMapPort` or `etherViewPort`.

## Visualization Architecture

The Eth Explorer services are automatically injected into the generated `docker-compose.yml` by the Docker compiler. The core services are:

| Service | Purpose |
| --- | --- |
| `seedemu-ethexplorer-web` | Built from `eth2-beaconchain-explorer`; provides the web UI, search, validator tagging, and frontend data updates |
| `seedemu-ethexplorer-backend` | Built from `beaconchain/backend`; handles execution-layer and consensus-layer indexing, statistics, and schema initialization |
| `postgres` | Stores explorer business data and indexed results |
| `clickhouse` | Stores analytical chain data for efficient statistical queries |
| `redis` | Provides cache and session storage |
| `littlebigtable` / `rawbigtable` | Emulate Bigtable storage to preserve compatibility with the original projects' data access model |

The compiler looks for nodes whose display names contain `Geth-` and `Beacon-` in the emulated topology. The first matched nodes are used as the explorer data sources and passed to the explorer through environment variables:

- `EL_HOST`: The execution-layer Geth node address.
- `CL_HOST`: The consensus-layer Beacon node address.

The explorer containers are attached to `beacon-network` and to the relevant emulated networks, so they can access both the database services and the Geth/Beacon nodes inside the emulation.

## Docker Image Layout

The customized image files are located in [`docker_images/seedemu-ethexplorer`](../../../docker_images/seedemu-ethexplorer):

```text
docker_images/seedemu-ethexplorer
├── docker-compose.yml
├── beaconchain/backend
│   ├── Dockerfile
│   ├── start.sh
│   └── local_deployment/
└── eth2-beaconchain-explorer
    ├── Dockerfile
    ├── start.sh
    └── local-deployment/
```

Key files:

- `eth2-beaconchain-explorer/Dockerfile` builds `explorer`, `validator-tagger`, and `frontend-data-updater`.
- `eth2-beaconchain-explorer/start.sh` generates the runtime configuration and starts the web service and frontend data update tasks.
- `beaconchain/backend/Dockerfile` builds the `bc` backend command.
- `beaconchain/backend/start.sh` initializes Bigtable, Postgres, and ClickHouse schemas, then starts `eth1indexer`, `rewards-exporter`, `statistics`, and other backend tasks.
- The two `provision-explorer-config-custom.sh` scripts dynamically generate `config.yml` from the emulated chain, including the genesis timestamp, genesis validators root, chain ID, network ID, Geth RPC address, and Beacon API address.

These images are referenced by the SEED Emulator Docker compiler as:

```python
SEEDEMU_ETH_EXPLORER_BACKEND_IMAGE = "handsonsecurity/seedemu-ethexplorer-backend:1.0"
SEEDEMU_ETH_EXPLORER_WEB_IMAGE = "handsonsecurity/seedemu-ethexplorer-web:1.0"
```

## Explorer Features

The Eth Explorer UI is based on the open-source Beacon Chain Explorer and is configured for the local test chain. Common features include:

- Viewing the overall chain status, such as the current epoch, slot, finalized/checkpoint state, and participation rate.
- Inspecting Beacon Chain slots and blocks, including proposer, block root, state root, attestations, and other consensus-layer data.
- Viewing the validator list and individual validator details, including activation status, balance, proposal activity, and attestation performance.
- Inspecting execution-layer blocks and transactions to understand data produced and synchronized by Geth nodes.
- Searching by slot, epoch, block, validator, and other chain objects.
- Viewing aggregated statistics, such as validator, block, reward, and chain activity data.

Immediately after the emulation starts, the explorer may not show complete data. This is expected while Geth, Beacon nodes, database schema initialization, and indexing tasks are still starting. Wait for several slots or check the `seedemu_ethexplorer_backend` and `seedemu_ethexplorer_web` logs to confirm indexing progress.

## Relationship with Internet Map

This example enables two visualization layers:

- Internet Map focuses on the network emulation layer. It is useful for observing AS topology, host placement, container status, and network connectivity.
- Eth Explorer focuses on the blockchain application layer. It is useful for observing execution-layer and consensus-layer data of the PoS private chain.

Using both together helps debug experiments from both the network and blockchain perspectives. For example, if a Beacon node appears abnormal in the explorer, users can switch to the Internet Map to inspect the AS, host state, and network connectivity of that node.

## Troubleshooting

### Cannot access `http://127.0.0.1:5000`

First check whether `output/docker-compose.yml` contains `seedemu-ethexplorer-web`, and confirm that the containers are running:

```bash
docker compose ps
```

If port `5000` is already occupied, update `Docker(etherViewPort=...)` and regenerate the output.

### The explorer starts but shows no chain data

This is common during the early stage of a local private-chain startup. The explorer needs to wait for:

- Geth HTTP RPC to become available.
- Beacon API to become available.
- Genesis information to be readable.
- Postgres, ClickHouse, Redis, and Bigtable emulator services to become ready.
- Backend indexing tasks to finish initialization and begin writing data.

Check logs to locate startup or indexing issues:

```bash
docker compose logs -f seedemu-ethexplorer-backend
docker compose logs -f seedemu-ethexplorer-web
```
