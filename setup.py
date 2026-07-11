from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


ROOT = Path(__file__).resolve().parent


class BuildPy(_build_py):
    """Copy canonical skill trees into package-owned wheel resources."""

    def run(self):
        super().run()
        destination = Path(self.build_lib) / "observer_kit" / "_skills"
        for name in ("observer-kit", "observer-flow"):
            bundled_skill = destination / name
            if bundled_skill.exists():
                shutil.rmtree(bundled_skill)
            shutil.copytree(
                ROOT / "skills" / name,
                bundled_skill,
                ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]", ".DS_Store"),
            )


setup(cmdclass={"build_py": BuildPy})
