from .DockerImage import DockerImage
from seedemu.core import BaseSystem
from enum import Enum

UBUNTU_IMAGE   = DockerImage(name='ubuntu:24.04',
                                software=[],
                                subset=None)

BASE_IMAGE     = DockerImage(name='handsonsecurity/seedemu-base:2.0',
                                software=['zsh', 'curl', 'nano', 'vim-nox', 'mtr-tiny', 'iproute2',
                                        'iputils-ping', 'tcpdump', 'termshark', 'dnsutils', 'jq', 'ipcalc', 'netcat-openbsd'],
                                subset=UBUNTU_IMAGE)

ROUTER_IMAGE   = DockerImage(name='handsonsecurity/seedemu-router:2.0',
                                software=['bird2'],
                                subset=BASE_IMAGE)

ETHEREUM_IMAGE = DockerImage(name='handsonsecurity/seedemu-ethereum',
                                software=['software-properties-common', 'python3', 'python3-pip'],
                                subset=BASE_IMAGE)

ETHEREUM_IMAGE_LEGACY = DockerImage(name='handsonsecurity/seedemu-ethereum:legacy',
                                software=['software-properties-common', 'python3', 'python3-pip'],
                                subset=BASE_IMAGE)

ETHEREUM_IMAGE_POS = DockerImage(name='handsonsecurity/seedemu-ethereum:pos2.0',
                                software=['software-properties-common', 'python3', 'python3-pip'],
                                subset=BASE_IMAGE)

MONERO_IMAGE = DockerImage(name='handsonsecurity/seedemu-monero:latest', software=[], subset=BASE_IMAGE)

# Agave (Solana) is packaged as a multiarch image. The image contains the
# pinned upstream binaries for amd64 and arm64 from docker_images/seedemu-solana.
SOLANA_IMAGE = DockerImage(name='handsonsecurity/seedemu-solana:1.0', software=[], local=False, subset=BASE_IMAGE)

OP_STACK_IMAGE = DockerImage(name='huagluck/seedemu-op-stack', software=[], subset=BASE_IMAGE)

SC_DEPLOYER_IMAGE = DockerImage(name='huagluck/seedemu-sc-deployer', software=[], subset=BASE_IMAGE)

CHAINLINK_IMAGE = DockerImage(name='amanvelani/chainlink-develop:amd64',
                                software=[],
                                subset=None)

UBUNTU_IMAGE_ARM64   = DockerImage(name='ubuntu:20.04',
                                software=[],
                                subset=None)

BASE_IMAGE_ARM64     = DockerImage(name='handsonsecurity/seedemu-base:2.0',
                                software=['zsh', 'curl', 'nano', 'vim-nox', 'mtr-tiny', 'iproute2',
                                        'iputils-ping', 'tcpdump', 'termshark', 'dnsutils', 'jq', 'ipcalc', 'netcat-openbsd'],
                                subset=UBUNTU_IMAGE_ARM64)

ROUTER_IMAGE_ARM64   = DockerImage(name='handsonsecurity/seedemu-router:2.0',
                                software=['bird2'],
                                subset=BASE_IMAGE_ARM64)

ETHEREUM_IMAGE_ARM64 = DockerImage(name='handsonsecurity/seedemu-ethereum-arm64',
                                software=['software-properties-common', 'python3', 'python3-pip'],
                                subset=BASE_IMAGE_ARM64)

ETHEREUM_IMAGE_ARM64_LEGACY = DockerImage(name='handsonsecurity/seedemu-ethereum-arm64:legacy',
                                software=['software-properties-common', 'python3', 'python3-pip'],
                                subset=BASE_IMAGE_ARM64)

ETHEREUM_IMAGE_ARM64_POS = DockerImage(name='handsonsecurity/seedemu-ethereum:pos2.0',
                                software=['software-properties-common', 'python3', 'python3-pip'],
                                subset=BASE_IMAGE_ARM64)

MONERO_IMAGE_ARM64 = DockerImage(name='handsonsecurity/seedemu-monero:latest', software=[], subset=BASE_IMAGE_ARM64)

# The same multiarch Solana image is used for arm64.
SOLANA_IMAGE_ARM64 = DockerImage(name='handsonsecurity/seedemu-solana:1.0', software=[], local=False, subset=BASE_IMAGE_ARM64)

OP_STACK_IMAGE_ARM64 = DockerImage(name='huagluck/seedemu-op-stack', software=[], subset=BASE_IMAGE_ARM64)

SC_DEPLOYER_IMAGE_ARM64 = DockerImage(name='huagluck/seedemu-sc-deployer', software=[], subset=BASE_IMAGE_ARM64)

CHAINLINK_IMAGE_ARM64 = DockerImage(name='amanvelani/chainlink-develop:arm64',
                                software=[],
                                subset=None)

BASESYSTEM_DOCKERIMAGE_MAPPING = {
        BaseSystem.UBUNTU_20_04:           UBUNTU_IMAGE,
        BaseSystem.SEEDEMU_BASE:           BASE_IMAGE,
        BaseSystem.SEEDEMU_ROUTER:         ROUTER_IMAGE,
         BaseSystem.SEEDEMU_ETHEREUM:       ETHEREUM_IMAGE,
        BaseSystem.SEEDEMU_ETHEREUM_LEGACY:       ETHEREUM_IMAGE_LEGACY,
        BaseSystem.SEEDEMU_ETHEREUM_POS:       ETHEREUM_IMAGE_POS,
        BaseSystem.SEEDEMU_MONERO:        MONERO_IMAGE,
        BaseSystem.SEEDEMU_SOLANA:        SOLANA_IMAGE,
        BaseSystem.SEEDEMU_OP_STACK:       OP_STACK_IMAGE,
        BaseSystem.SEEDEMU_SC_DEPLOYER:    SC_DEPLOYER_IMAGE,
        BaseSystem.SEEDEMU_CHAINLINK:      CHAINLINK_IMAGE
}

BASESYSTEM_ARM64_DOCKERIMAGE_MAPPING = {
        BaseSystem.UBUNTU_20_04:        UBUNTU_IMAGE_ARM64,
        BaseSystem.SEEDEMU_BASE:        BASE_IMAGE_ARM64,
        BaseSystem.SEEDEMU_ROUTER:      ROUTER_IMAGE_ARM64,
        BaseSystem.SEEDEMU_ETHEREUM:    ETHEREUM_IMAGE_ARM64,
        BaseSystem.SEEDEMU_ETHEREUM_LEGACY:       ETHEREUM_IMAGE_ARM64_LEGACY,
        BaseSystem.SEEDEMU_ETHEREUM_POS:       ETHEREUM_IMAGE_ARM64_POS,
        BaseSystem.SEEDEMU_MONERO:        MONERO_IMAGE_ARM64,
        BaseSystem.SEEDEMU_SOLANA:        SOLANA_IMAGE_ARM64,
        BaseSystem.SEEDEMU_OP_STACK:    OP_STACK_IMAGE_ARM64,
        BaseSystem.SEEDEMU_SC_DEPLOYER: SC_DEPLOYER_IMAGE_ARM64,
        BaseSystem.SEEDEMU_CHAINLINK:   CHAINLINK_IMAGE_ARM64
}

class Platform(Enum):
    """!
    @brief Platform Enum.
    """

    ARM64 = 'arm64'
    AMD64 = 'amd64'

BASESYSTEM_DOCKERIMAGE_MAPPING_PER_PLATFORM = {
    Platform.AMD64:     BASESYSTEM_DOCKERIMAGE_MAPPING,
    Platform.ARM64:     BASESYSTEM_ARM64_DOCKERIMAGE_MAPPING
}
