from setuptools import setup

setup(
    package_data={
        "observer_kit": [
            "assets/*.js",
            "EXPLAIN.md",
        ],
    },
    include_package_data=True,
)
