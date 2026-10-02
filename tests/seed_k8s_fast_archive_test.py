#!/usr/bin/env python3

import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class SeedK8sFastArchiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        module_path = repo_root / "largeScale/b62_k8s_scale/fastSeedemuImageArchive.py"
        spec = importlib.util.spec_from_file_location("fast_seedemu_image_archive", module_path)
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def writeDockerfile(self, directory: Path, content: str) -> Path:
        path = directory / "Dockerfile"
        path.write_text(textwrap.dedent(content).strip() + "\n", encoding="utf-8")
        return path

    def test_software_install_becomes_shared_base_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dockerfile = self.writeDockerfile(
                Path(tmp),
                """
                FROM router-base
                ARG DEBIAN_FRONTEND=noninteractive
                RUN apt-get update && apt-get install -y --no-install-recommends iperf3
                RUN mkdir -p /usr/share/doc/bird2/examples/
                RUN touch /usr/share/doc/bird2/examples/bird.conf
                COPY start-hash /start.sh
                RUN chmod +x /start.sh
                CMD ["/start.sh"]
                """,
            )
            self.assertEqual(
                self.module.dockerfile_build_instructions(dockerfile),
                (
                    "ARG DEBIAN_FRONTEND=noninteractive",
                    "RUN apt-get update && apt-get install -y --no-install-recommends iperf3",
                ),
            )

    def test_standard_seedemu_operations_need_no_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dockerfile = self.writeDockerfile(
                Path(tmp),
                """
                FROM router-base
                ARG DEBIAN_FRONTEND=noninteractive
                RUN mkdir -p /usr/share/doc/bird2/examples/
                RUN touch /usr/share/doc/bird2/examples/bird.conf
                COPY start-hash /start.sh
                RUN chmod +x /start.sh
                CMD ["/start.sh"]
                """,
            )
            self.assertEqual(
                self.module.dockerfile_build_instructions(dockerfile),
                (),
            )

    def test_run_after_copy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dockerfile = self.writeDockerfile(
                Path(tmp),
                """
                FROM router-base
                COPY custom-script /custom-script
                RUN /custom-script
                """,
            )
            with self.assertRaisesRegex(RuntimeError, "unsupported RUN after COPY"):
                self.module.dockerfile_build_instructions(dockerfile)


if __name__ == "__main__":
    unittest.main()
