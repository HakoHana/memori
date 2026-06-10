"""Flat layout: 仓库根目录 = memori 包

pip install -e . 将根目录注册为 memori 包，
core/ 等子目录映射为 memori.core 等子包。
"""
from setuptools import setup

setup(
    package_dir={
        "memori": ".",
        "memori.core": "core",
        "memori.storage": "storage",
        "memori.models": "models",
        "memori.retrieval": "retrieval",
        "memori.api": "api",
    },
    packages=[
        "memori",
        "memori.core",
        "memori.storage",
        "memori.models",
        "memori.retrieval",
        "memori.api",
    ],
)
