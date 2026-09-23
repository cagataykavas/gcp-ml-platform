"""Beam setup file for staging the local event-admission package on Dataflow."""

from setuptools import find_packages, setup

setup(
    name="gcp-ml-platform-contracts",
    version="0.1.0",
    description="Event contracts for the GCP ML platform reference pipeline",
    packages=find_packages(include=("gcpml", "gcpml.*")),
    python_requires=">=3.11",
)
