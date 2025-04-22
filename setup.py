#!/usr/bin/env python

from setuptools import setup

setup(
    name="discord-to-blog",
    version="0.0.1",
    description="A Discord bot that generates a static blog from posts in a channel",
    url="https://github.com/pR0Ps/discord-to-blog",
    license="GPLv3",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.9",
    install_requires = [
        "discord.py>=2.0.0,<3.0.0",
        "Markdown>=3.3.3,<4.0.0",
        "pelican>=4.5.4,<5.0.0",
        "pyyaml>=5.4.1,<7.0.0",
        "pytz",
        "setuptools",
    ],
    packages=["discord_to_blog"],
    package_data={"discord_to_blog": ["theme/*", "theme/*/*", "theme/*/*/*"]}, # ugh
    entry_points={"console_scripts": ["discord-to-blog=discord_to_blog.__main__:main"]},
)
