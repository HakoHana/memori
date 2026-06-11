from setuptools import setup
setup(
    package_dir={"memori": "memori"},
    packages=[
        "memori", "memori.core", "memori.storage",
        "memori.models", "memori.retrieval", "memori.api",
        "memori.pipeline", "memori.features", "memori.utils",
        "adapters", "adapters.astrbot",
    ],
)
