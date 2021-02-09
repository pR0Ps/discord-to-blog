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
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
    ],
    install_requires = [
        "discord.py>=1.5.0,<2.0.0",
        "Markdown>=3.3.3,<4.0.0",
        "pelican>=4.5.4,<5.0.0",
        "pyyaml>=5.4.1,<6.0.0",
        "pytz",
    ],
    packages=["discord_to_blog"],
    entry_points={"console_scripts": ["discord-to-blog=discord_to_blog.__main__:main"]},
)
