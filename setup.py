from setuptools import setup, find_packages

setup(
    name="sentinel",
    version="1.0.0",
    description="Hardware-Aware Adaptive AI for Samsung Galaxy Devices",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Project Sentinel",
    license="Apache-2.0",
    python_requires=">=3.10",
    packages=find_packages(include=["bridge", "agent", "dashboard", "scripts"]),
    install_requires=[
        "anthropic>=0.25.0",
    ],
    extras_require={
        "dev": ["pytest>=8.0.0"],
        "full": ["watchdog>=4.0.0", "psutil>=5.9.0"],
    },
    entry_points={
        "console_scripts": [
            "sentinel-agent=agent.sentinel_agent:run_repl",
            "sentinel-api=scripts.sentinel_api:main",
            "sentinel-replay=scripts.replay:main",
            "sentinel-bench=scripts.benchmark:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: System :: Hardware",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
)
